from jira import JIRA
from slacksocket import SlackSocket
from threading import Thread
import Queue
from db import *
import re
from datetime import time

config = {}
execfile("config.py", config)
slack_sock = SlackSocket(config["slack_token"], True, ["message"])


# TODO: Should possibly refactor
class Session(object):
    def __init__(self, username):
        self.__queue = Queue.Queue()
        channel = slack_sock.get_im_channel(username)
        self.__channel_id = channel["id"]
        try:
            rec = User.get(User.username == username)
        except User.DoesNotExist:
            rec = User.create(username=username)
        self.__user = rec
        self.active = False
        self.__silent_exit = True

    def queue_message(self, message):
        self.__queue.put(message)

    def start_worker(self):
        self.active = True
        t = Thread(target=self.__process_message)
        t.daemon = True
        t.start()

    def __process_message(self):
        try:
            message = self.__queue.get(True, 120)
            if message == "activate":
                self.__activate_user()
            if self.__user.active:
                message = message.lower()
                self.__lookup_action(message)

            else:
                self.__send_message("Your user appears to be inactive, you have either disabled it or have not "
                                    "initialized it.  If you just want to activate it with the current settings"
                                    "(defaults are 9-5 lunch 12-1), reply with 'activate'.  To learn how to set hours,"
                                    " reply with 'help'.")

        except Queue.Empty:
            if not self.__silent_exit:
                slack_sock.send_msg("Timed out", channel_id=self.__channel_id)
        finally:
            self.active = False

    def __activate_user(self):
        self.__user.active = True
        self.__user.save()
        self.__send_message("User activated")

    def __lookup_action(self, message):
        if message == "help":
            self.__show_help()

        if message == "inactivate":
            self.__inactivate_user()

        if re.search("^(?:get|show) (?:hours|options|settings)$", message):
            self.__show_opts()

        # set hours check
        match = re.search("^set hours (\d{1,2})(?::(\d{1,2}))? ?(am|pm)? ?- ?(\d+)(?::(\d{1,2}))? ?(am|pm)?$", message)
        if match:
            self.__set_hours(time(hour=int(match.group(1)) if match.group(3) != "pm" else int(match.group(1)) + 12,
                                  minute=match.group(2) if match.group(2) else 0),
                             time(hour=int(match.group(4)) + 12 if match.group(6) != "am" else int(match.group(4)),
                                  minute=match.group(5) if match.group(5) else 0))
            return

        # set lunch hours check
        match = re.search("^set lunch hours (\d{1,2})(?::(\d{1,2}))? ?(am|pm)? ?- ?(\d+)(?::(\d{1,2}))? ?(am|pm)?$",
                          message)
        if match:
            self.__set_lunch_hours(time(hour=int(match.group(1)) if match.group(3) != "pm"
                                        else int(match.group(1)) + 12,
                                        minute=match.group(2) if match.group(2) else 0),
                                   time(hour=int(match.group(4)) + 12 if match.group(6) != "am"
                                        else int(match.group(4)),
                                        minute=match.group(5) if match.group(5) else 0))
            return

    def __set_hours(self, start, end):
        self.__user.on_time = start
        self.__user.off_time = end
        self.__user.save()
        self.__send_message("Hours set")

    def __inactivate_user(self):
        self.__user.active = False
        self.__user.save()
        self.__send_message("User deactivated")

    def __send_message(self, message):
        slack_sock.send_msg(message, channel_id=self.__channel_id, confirm=False)

    def __show_opts(self):
        self.__send_message("Active\n"
                            "Work hours -- {0} - {1}\n"
                            "Lunch hours -- {2} - {3}".format(self.__user.on_time.strftime("%I:%M %p"),
                                                              self.__user.off_time.strftime("%I:%M %p"),
                                                              self.__user.lunch_on.strftime("%I:%M %p"),
                                                              self.__user.lunch_off.strftime("%I:%M %p")))

    def __show_help(self):
        self.__send_message("-- gudbot help --\n"
                            "activate -- activate user\n"
                            "set hours HH(:MM)?(AM|PM)?-HH(:MM)?(AM|PM)? -- set start and stop hours\n"
                            "set lunch hours HH(:MM)?(AM|PM)?-HH(:MM)?(AM|PM)? -- set lunch start and stop hours\n"
                            "get hours -- show work and lunch hours")

    def __set_lunch_hours(self, start, end):
        self.__user.lunch_on = start
        self.__user.lunch_off = end
        self.__user.save()
        self.__send_message("Lunch hours set")


def main():
    active_sessions = {}

    while True:
        event = slack_sock.get_event().event
        print event
        if not ("hidden" in event and event["hidden"]) and event["user"] == event["channel"] and \
                event["user"] != "slackbot":
            print "{0} - {1}".format(event["user"], event["text"])
            if event["user"] in active_sessions and active_sessions[event["user"]].active:
                active_sessions[event["user"]].queue_message(event["text"])
            else:
                session = Session(event["user"])
                session.queue_message(event["text"])
                session.start_worker()
                active_sessions[event["user"]] = session

if __name__ == "__main__":
    main()
