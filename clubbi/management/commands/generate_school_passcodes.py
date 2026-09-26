from django.core.management.base import BaseCommand
from werkzeug.security import generate_password_hash

from clubbi.views import db_connection, ensure_schema, generate_school_passcode


class Command(BaseCommand):
    help = "Generate passcodes for legacy schools that do not have one."

    def handle(self, *args, **options):
        ensure_schema()
        db = db_connection()
        schools = db.execute("SELECT id, name FROM schools WHERE school_passcode_hash IS NULL OR school_passcode_hash = ''").fetchall()
        for school in schools:
            passcode = generate_school_passcode()
            db.execute("UPDATE schools SET school_passcode_hash = ? WHERE id = ?", (generate_password_hash(passcode), school["id"]))
            self.stdout.write(f"{school['name']}: {passcode}")
        db.commit()
        db.close()
        if not schools:
            self.stdout.write("All schools already have passcodes.")