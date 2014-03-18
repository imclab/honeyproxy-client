from wsgiref.simple_server import make_server

import bottle, json, threading, time, subprocess, collections, sys, os, datetime, IPy, re, time, urllib
from bottle import Bottle, static_file, request, redirect, PasteServer as wsgiserver, response
from bottle.ext import sqlalchemy as bottlealchemy
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import create_engine, Column, DateTime, String, Boolean, Integer, Enum
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import object_session
from sqlalchemy.pool import QueuePool
from recaptcha.client import captcha
from subprocess import PIPE, STDOUT

with open("config.json","r") as f:
    config = json.loads(f.read())

if not os.path.isdir(config["logdir"]):
    os.makedirs(config["logdir"])

engine = create_engine('sqlite:///honeyproxy.sqlite', echo=False)
Session = sessionmaker(bind=engine)
#session = Session()
Base = declarative_base()

class Analysis(Base):
    __tablename__ = 'analysis'
    id = Column(String, primary_key=True, default=lambda: os.urandom(8).encode('hex'))
    url = Column(String, nullable=False)
    request_count = Column(Integer)
    submit_time = Column(DateTime, default=datetime.datetime.now)
    status = Column(Enum("QUEUE","ACTIVE","FINISHED"), default="QUEUE")

    @property
    def analysis_size(self):
        if self.status == "QUEUE" or not os.path.isfile(self.getDumpfileLocation()):
            return 0
        else:
            return os.path.getsize(self.getDumpfileLocation())
    @property
    def queue_position(self):
        if self.status != "QUEUE":
            return -1
        return 1 + object_session(self).query(Analysis).filter_by(status="QUEUE").filter(Analysis.submit_time < self.submit_time).count()
    
    def getDumpfileLocation(self):
        return os.path.join(config["dumpdir"],self.id) 
    def __repr__(self):
        return "<Analysis('%s','%s','%s')>" % (self.id,self.url,str(self.status))
    def as_dict(self):
        ret = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        ret["analysis_size"]  = self.analysis_size
        ret["queue_position"] = self.queue_position
        return ret
    def as_htmlsafe_dict(self):
        ret = self.as_dict()
        ret["submit_time"] = ret["submit_time"].isoformat()
        ret["url"] = urllib.quote(ret["url"],safe='~@#$&()*!+=:;,.?/\'')
        return ret

    __mapper_args__ = {
        'order_by' :[submit_time.desc()]
    }
   

Base.metadata.create_all(engine)

bottle.debug(config["debug"])

app = Bottle()
app.install(bottlealchemy.Plugin(engine,keyword='session'))

template_env = Environment(loader=FileSystemLoader("./templates"))


@app.route('/static/<filepath:path>')
def serve_static(filepath):
    resp = static_file(filepath, root='./static')
    resp.set_header('Cache-Control','public, max-age=3600')
    return resp


@app.route('/favicon.ico')
def favicon():
    return static_file('/favicon.ico', root='./static')

@app.route('/')
def main(session):
    analyses = session.query(Analysis).filter(Analysis.status != "QUEUE").slice(0,20).all()
    analyses = json.dumps(list(a.as_htmlsafe_dict() for a in analyses))
    template = template_env.get_template('index.html')
    return template.render(analyses=analyses)

@app.route('/analysis/<analysis_id:re:[a-z0-9]+>')
def analysis(analysis_id,session):
    analysis = session.query(Analysis).get(analysis_id)
    if not analysis:
        abort(404, "No such analysis.")
    if analysis.status == "QUEUE":
        template = template_env.get_template('queue.html')
        return template.render(data=json.dumps(api_analysis(analysis_id)))
    else:
        instance = instanceManager.getInstance(analysis)
        template = template_env.get_template('appframe.html')
        return template.render(
            url="http://"+request.urlparts.netloc.split(":")[0]+":"+str(instance.guiport)+"/app/",
            analysis_url=analysis.url)        

@app.post('/api/search')
def api_search(session):
    url = request.forms.get('url')
    if url:
        matches = session.query(Analysis).filter(
            Analysis.url.like("%"+request.forms.get('url')+"%")).slice(0,50).all()
        return {"results": list(a.as_htmlsafe_dict() for a in matches)}
    else:
        return{"error":"no url specified"}

@app.route('/api/analysis/<analysis_id:re:[a-z0-9]+>')
def api_analysis(analysis_id,session):
    analysis = session.query(Analysis).get(analysis_id)
    if not analysis:
        return {"error":"not found"}
    else:
        return analysis.as_htmlsafe_dict()

@app.post('/api/analyze')
def api_analyze(session):
    response = captcha.submit(
        request.forms.get(r'recaptcha_challenge_field'),
        request.forms.get('recaptcha_response_field'),
        config["recaptcha"]["privatekey"],
        request.environ.get('REMOTE_ADDR')
        )
    if not response.is_valid:
        return {"success": False, "msg": "invalid captcha"}
    else:
        url = request.forms.get("url")
        if not re.match("^(https?://)?[a-zA-Z0-9_\\-\\.]+\\.[a-zA-Z0-9]+(:\d+)?(/.*)?$",url):
            return {"success": False, "msg": "invalid url"}
        if not url.lower().startswith("http"):
            url = "http://"+url
        analysis = Analysis(url=url)
        session.add(analysis)
        session.commit()
        return analysis.as_htmlsafe_dict()

# Notice: The code below is a proof of concept.
# It is incredibly hacky and badly designed. Spawning N instances of HoneyProxy is downright silly.
# So, why did I commit this crime?
# The HoneyProxy dump format will get a complete overhaul very soon.
# Anything I implemented here will get obsolete with these changes,
# so this code just serves us as a bad PoC. Not as a base to build on.

class InstanceInfo(object):
    def __init__(self, instance_type, handle, apiport, guiport):
        self.instance_type = instance_type
        self.handle        = handle
        self.apiport       = apiport
        self.guiport       = guiport
        self.starttime     = time.time()
        self.last_access   = self.starttime
    def __repr__(self):
        return "Instance<%s,%d,%d,%d>" % (self.instance_type, self.apiport, self.guiport, self.starttime)
        

class HoneyProxyInstanceManager(object):
    def __init__(self):
        self.active = {}
        self.ports = set((8200+i for i in range(0,800)))
    def getInstance(self, analysis):
        if analysis.id in self.active:
            instanceInfo = self.active[analysis.id]
            instanceInfo.last_access = time.time()
            return instanceInfo
        return self.spawnInstance(analysis,"result")
    def _getPorts(self, apiport=None, guiport=None):
        if apiport:
            self.ports.remove(apiport)
        else:
            apiport = self.ports.pop()

        if guiport:
            self.ports.remove(guiport)
        else:
            guiport = self.ports.pop()
        return apiport, guiport
    
    def spawnInstance(self, analysis, instance_type, apiport=None, guiport=None):
        apiport, guiport = self._getPorts(apiport, guiport)
        print "Spawn %s instance(%d, %d)..." % (instance_type, apiport, guiport)

        args = (config["HoneyProxy"] +
                [
                    "--api-auth", "NO_AUTH",
                    "--apiport", str(apiport),
                    "--guiport", str(guiport),
                    "--no-gui",
                    "--readonly"
                ])
        if instance_type == "listener":
            args.extend(
                [
                    "-w", analysis.getDumpfileLocation(),
                    "-p","8100",
                    "-T",
                    "-s","./resources/suppresswinupdate.py",
                    "-Z","5m"])
        else:
            args.extend(
                [
                    "-r", analysis.getDumpfileLocation(),
                    "-n",
                    "--readonly"])
        p = subprocess.Popen(args,
                             stdout=PIPE,
                             stderr=STDOUT)
        out = p.stdout.readline()
        if out != "HoneyProxy has been started!\n":
            raise RuntimeError("Couldn't start HoneyProxy: %s" % out+p.stdout.read())
        self.active[analysis.id] = InstanceInfo(instance_type, p, apiport, guiport)
        return self.active[analysis.id]
    
    def terminateProcess(self, analysis):
        data = self.active[analysis.id]
        logfile = os.path.join(config["logdir"], analysis.id + ".log")
        data.handle.terminate()
        with open(logfile, "a") as f:
            f.write("\n\n"+str(data)+"\n\n")
            f.write(data.handle.stdout.read())        
        
        self.ports.add(data.apiport)
        self.ports.add(data.guiport)
        
        del self.active[analysis.id]
        

class RequestHandler(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.session = Session()
        self.daemon = True
    def run(self):
        while True:
            last_allowed_timestamp = time.time() - config["instance_lifetime"]
            for analysis_id, instanceInfo in instanceManager.active.items():
                if instanceInfo.last_access < last_allowed_timestamp and instanceInfo.instance_type == "result":
                    print "Terminating result instance %s" % analysis_id
                    analysis = self.session.query(Analysis).get(analysis_id)
                    instanceManager.terminateProcess(analysis)
            analysis = self.session.query(Analysis).filter_by(status="QUEUE").order_by(Analysis.submit_time).first()
            if analysis == None:
                time.sleep(1)
            else:
                #Launch HoneyProxy
                instanceManager.spawnInstance(analysis,"listener")

                analysis.status = "ACTIVE"
                self.session.commit()
                
                # Reset VM and start it
                try:
                    subprocess.call(config["VBoxManage"] + ["controlvm", config["VMName"], "poweroff"])
                except:
                    pass
                subprocess.call(config["VBoxManage"] + ["snapshot", config["VMName"], "restorecurrent"])
                subprocess.check_call(config["VBoxManage"] + ["startvm", config["VMName"],"--type","headless"])
                
                #Launch URL
                params = (config["VBoxManage"] + ["guestcontrol", config["VMName"], "exec"]
                                + config["VMStart"] + [analysis.url])
                subprocess.call(params)

                print "Site opened."
                time.sleep(config["analysis_duration"])

                #Shut down VM
                subprocess.call(config["VBoxManage"] + ["controlvm", config["VMName"], "poweroff"])

                #Finish HoneyProxy
                # dirty workaround: terminate the instance to free up the proxy server.
                # restart a resultinstance on the same ports afterwards
                instanceInfo = instanceManager.getInstance(analysis)
                instanceManager.terminateProcess(analysis)
                instanceManager.spawnInstance(analysis, "result", instanceInfo.apiport, instanceInfo.guiport)
                
                analysis.status = "FINISHED"
                self.session.commit()
                

instanceManager = HoneyProxyInstanceManager()
request_queue = collections.deque(maxlen=50)
requesthandler = RequestHandler().start()

if __name__ == "__main__":
    app.run(reloader=config["debug"],server=wsgiserver, port=config["port"],host="0.0.0.0")
