# flake8: noqa: F401
from flask import Flask

from localtileserver.tileserver import rest, urls, views
from localtileserver.tileserver.blueprint import cache, tileserver
from localtileserver.tileserver.utilities import get_cache_dir, make_vsi, purge_cache


def create_app(url_prefix="/"):
    app = Flask(__name__)
    cache.init_app(app)
    app.register_blueprint(tileserver, url_prefix=url_prefix)
    return app