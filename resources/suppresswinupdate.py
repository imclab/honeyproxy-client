def request(context, flow):
    if (flow.request.host == "www.download.windowsupdate.com"
        and flow.request.path == "/msdownload/update/v3/static/trustedr/en/authrootseq.txt"):
        flow.kill(context) #suppress flows of the windows update check
