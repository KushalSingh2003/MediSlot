import os

from flask import Flask, redirect, url_for
from flask_session import Session

from config import Config

from routes.auth import auth_bp
from routes.dashboard import dashboard_bp
from routes.profile import profile_bp
from routes.consultation_types import consultation_types_bp
from routes.schedule import schedule_bp
from routes.appointments import appointments_bp
from routes.blackouts import blackouts_bp
from routes.patient_portal import patient_bp





def create_app():

    app = Flask(__name__)

    app.config.from_object(Config)

    os.makedirs("instance/flask_session", exist_ok=True)

    Session(app)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(consultation_types_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(appointments_bp)
    app.register_blueprint(blackouts_bp)
    app.register_blueprint(patient_bp)

    @app.route("/")
    def index():
        from flask import render_template
        return render_template("landing.html")

    @app.route("/health")
    def health():
        return {"status": "running"}

    return app


app = create_app()


if __name__ == "__main__":
    print(app.url_map)

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )