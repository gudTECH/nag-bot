from peewee import *
from playhouse.sqlite_ext import SqliteExtDatabase
from datetime import time

db = SqliteExtDatabase("gudbot.db")


class BaseModel(Model):
    class Meta:
        database = db


class User(BaseModel):
    username = CharField(unique=True)
    on_time = TimeField(default=time(hour=9))
    off_time = TimeField(default=time(hour=5))
    lunch_on = TimeField(default=time(hour=12))
    lunch_off = TimeField(default=time(hour=13))
    active = BooleanField(default=False)


class Event(BaseModel):
    user = ForeignKeyField(User, related_name="events")
    tickets_affected = TextField()
    active = BooleanField(default=False)
