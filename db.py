from peewee import *
from playhouse.sqlite_ext import SqliteExtDatabase
from datetime import time
import json
from typing import List

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
    __tickets_affected = TextField(null=True)
    active = BooleanField(default=True)
    conflict_type = CharField()

    @property
    def tickets_affected(self) -> List[str]:
        return json.loads(self.__tickets_affected) if self.__tickets_affected else []

    @tickets_affected.setter
    def tickets_affected(self, val: List[str]):
        self.__tickets_affected = json.dumps(val)


class PrevTicket(BaseModel):
    user = ForeignKeyField(User, related_name="prev_tickets")
    ticket_key = CharField()
