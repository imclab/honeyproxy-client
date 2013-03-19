from wsgiref.simple_server import make_server

import bottle, json, threading, time, subprocess, collections, sys, os, datetime, IPy, re, time
from bottle import Bottle, static_file, request, redirect
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import create_engine, Column, DateTime, String, Boolean, Integer, Enum
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from recaptcha.client import captcha
from subprocess import PIPE, STDOUT

with open("config.json","r") as f:
    config = json.loads(f.read())

engine = create_engine('sqlite:///honeyproxy.sqlite', echo=False)
Session = sessionmaker(bind=engine)
session = Session()
Base = declarative_base()

class Analysis(Base):
    __tablename__ = 'analysis'
    id = Column(String, primary_key=True, default=lambda: os.urandom(8).encode('hex'))
    url = Column(String, nullable=False)
    request_count = Column(Integer)
    submit_time = Column(DateTime, default=datetime.datetime.now)
    status = Column(Enum("QUEUE","ACTIVE","FINISHED"), default="QUEUE")
    def __repr__(self):
        return "<Analysis('%s','%s','%s')>" % (self.id,self.url,str(self.status))
    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
    def as_primitive_dict(self):
        ret = self.as_dict()
        ret["submit_time"] = ret["submit_time"].isoformat()
        return ret
    def getQueuePosition(self):
        if self.status != "QUEUE":
            return -1
        return 1+session.query(Analysis).filter_by(status="QUEUE").filter(Analysis.submit_time < self.submit_time).count()

Base.metadata.create_all(engine)

bottle.debug(config["debug"])

app = Bottle()
template_env = Environment(loader=FileSystemLoader("./templates"))


@app.route('/static/<filepath:path>')
def server_static(filepath):
    return static_file(filepath, root='./static')


@app.route('/favicon.ico')
def favicon():
    return static_file('/favicon.ico', root='./static')

@app.route('/')
def thugme():
    analyses = session.query(Analysis).filter(Analysis.status != "QUEUE").order_by(Analysis.submit_time.desc()).slice(0,20).all()
    analyses = json.dumps(list(a.as_primitive_dict() for a in analyses))
    template = template_env.get_template('index.html')
    return template.render(analyses=analyses)

@app.route('/analysis/<analysis_id:re:[a-z0-9]+>')
def show_analysis(analysis_id=None):
    analysis = session.query(Analysis).get(analysis_id)
    if not analysis:
        abort(404, "No such analysis.")
    if analysis.status == "QUEUE":
        template = template_env.get_template('queue.html')
        return template.render(data=json.dumps(analysis_api(analysis_id)))
    else:
        instance = instanceManager.getInstance(analysis)
        template = template_env.get_template('appframe.html')
        return template.render(url="http://"+request.urlparts.netloc.split(":")[0]+":"+str(instance["guiport"])+"/app/")        

@app.post('/api/search')
def search():
    url = request.forms.get('url')
    if url:
        matches = session.query(Analysis).filter(
            Analysis.url.like("%"+request.forms.get('url')+"%")).all()
        return {"results": list(a.as_primitive_dict() for a in matches)}
    else:
        return{"error":"no url specified"}

@app.route('/api/analysis/<analysis_id:re:[a-z0-9]+>')
def analysis_api(analysis_id):
    analysis = session.query(Analysis).get(analysis_id)
    if not analysis:
        return {"error":"not found"}
    if analysis.status == "FINISHED":
        return {"id": analysis_id, "complete":True}
    else:
        return {"id": analysis_id, "complete":False, "queue": analysis.getQueuePosition()}

@app.post('/api/analyze')
def analyze():
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
        analysis = Analysis(url=url)
        session.add(analysis)
        session.commit()
        return {"success": True, "queue": analysis.getQueuePosition(), "id": analysis.id} #return some id, too.

# Notice: The code below is a proof of concept.
# It is incredibly hacky and badly designed. Spawning N instances of HoneyProxy is downright silly.
# So, why did I commit this crime?
# The HoneyProxy dump format will get a complete overhaul very soon.
# Anything I implemented here will get obsolete with these changes,
# so this code just serves us as a bad PoC. Not as a base to build on.

class HoneyProxyInstanceManager():
    def __init__(self):
        self.active = {}
        self.ports = set((8200+i for i in range(0,800)))
    def getInstance(self, analysis):
        if analysis.id in self.active:
            return self.active[analysis.id]
        return self.spawnResultInstance(analysis)
    def spawnListener(self, analysis):
        apiport = self.ports.pop()
        guiport = self.ports.pop()

        print "Spawn listener instance(%d, %d)..." % (apiport, guiport)
        
        p = subprocess.Popen(config["HoneyProxy"] + [
            "-w",os.path.join(config["dumpdir"],analysis.id),
            "-p","8100",
            "-T",
            "-Z","5m",
            "--api-auth", "NO_AUTH",
            "--apiport", str(apiport),
            "--guiport", str(guiport),
            "--no-gui",
            "--readonly"],
            stdout = subprocess.PIPE,
            stderr = subprocess.STDOUT)
        out = p.stdout.readline()
        if out != "HoneyProxy has been started!\n":
            raise RuntimeError("Couldn't start HoneyProxy: %s" % out+p.stdout.read())
        self.active[analysis.id] = { "handle": p, "apiport": apiport, "guiport": guiport }
        return self.active[analysis.id]
    def spawnResultInstance(self, analysis, apiport=None, guiport=None):
        if apiport:
            self.ports.remove(apiport)
        else:
            apiport = self.ports.pop()

        if guiport:
            self.ports.remove(guiport)
        else:
            guiport = self.ports.pop()

        print "Spawn result instance(%d, %d)..." % (apiport, guiport)

        args = config["HoneyProxy"] + [
            "-r", os.path.join(config["dumpdir"],analysis.id),
            "-n",
            "--api-auth", "NO_AUTH",
            "--apiport", str(apiport),
            "--guiport", str(guiport),
            "--no-gui",
            "--readonly"]
        print args
        p = subprocess.Popen(args,
            stdout = subprocess.PIPE,
            stderr = subprocess.STDOUT)
        out = p.stdout.readline()
        if out != "HoneyProxy has been started!\n":
            raise RuntimeError("Couldn't start HoneyProxy: %s" % out+p.stdout.read())
        else:
            print "Result instance spawned."
        self.active[analysis.id] = { "handle": p,
                                                "starttime": time.time(),
                                                "apiport": apiport,
                                                "guiport": guiport }        
        return self.active[analysis.id]
    def terminateProcess(self, analysis):
        self.active[analysis.id]["handle"].terminate()
        for port in ("apiport","guiport"):
            if port in self.active[analysis.id]:
                self.ports.add(self.active[analysis.id][port])
        del self.active[analysis.id]
        

class RequestHandler(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.session = Session()
        self.daemon = True
    def run(self):
        while True:
            analysis = self.session.query(Analysis).filter_by(status="QUEUE").order_by(Analysis.submit_time).first()
            if analysis == None:
                time.sleep(1)
            else:
                #Launch HoneyProxy
                instanceManager.spawnListener(analysis)

                analysis.status = "ACTIVE"
                self.session.commit()
                
                # Reset VM and start it
                subprocess.call([config["VBoxManage"], "controlvm", config["VMName"], "poweroff"])
                subprocess.call([config["VBoxManage"], "snapshot", config["VMName"], "restorecurrent"])
                subprocess.check_call([config["VBoxManage"], "startvm", config["VMName"],"--type","gui" if config["debug"] else "headless"])
                
                #Launch URL
                params = ([config["VBoxManage"],
                                 "guestcontrol", config["VMName"], "exec"]
                                + config["VMStart"] + [analysis.url])
                subprocess.call(params)

                print "Site opened."
                time.sleep(config["analysis_duration"])

                #Shut down VM
                subprocess.call([config["VBoxManage"], "controlvm", config["VMName"], "poweroff"])

                #Finish HoneyProxy
                # dirty workaround: terminate the instance to free up the proxy server.
                # restart a resultinstance on the same ports afterwards
                instOpts = instanceManager.getInstance(analysis)
                instanceManager.terminateProcess(analysis)
                instanceManager.spawnResultInstance(analysis, instOpts["apiport"], instOpts["guiport"])
                
                analysis.status = "FINISHED"
                self.session.commit()
                

instanceManager = HoneyProxyInstanceManager()
request_queue = collections.deque(maxlen=50)
requesthandler = RequestHandler().start()

if __name__ == "__main__":
    httpd = make_server('', 8000, app)
    print "Serving on port 8000..."
    httpd.serve_forever()
