import webob.dec
import json
from webob import Response


def dr_controller_factory(global_conf, **local_conf):
    return DrController(global_conf, local_conf)


class DrController(object):
    def __init__(self, global_conf, local_conf):
        self.global_conf = global_conf
        self.local_conf = local_conf

    @webob.dec.wsgify
    def __call__(self, req):
        res = Response()
        res.content_type = 'application/json'
        res.body = json.dumps(req.environ['updated_env'],
                              indent=4, sort_keys=True)
        return res