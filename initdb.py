from db import *

if __name__ == "__main__":
    db.connect()
    db.create_tables([User, Event, PrevTicket])
