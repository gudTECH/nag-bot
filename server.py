from jira import JIRA
from slacksocket import SlackSocket
from threading import Thread, Timer
import Queue
from db import *
import re
from datetime import time, datetime, timedelta
import sys

config = {}
execfile("config.py", config)
slack_sock = SlackSocket(config["slack_token"], True, ["message"])
active_sessions = {}
jira_conn = JIRA(server=config["jira_server"], basic_auth=(config["jira_user"], config["jira_pass"]))

# TODO: Should possibly refactor
class Session(object):
    def __init__(self, username):
        # type: (str) -> None
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
        self.__open_question = False

    def queue_message(self, message):
        # type: (str) -> None
        """Queue a message for processing"""
        self.__queue.put(message)

    def start_worker(self):
        # type: () -> None
        """Start worker thread"""
        self.active = True
        t = Thread(target=self.__process_message)
        t.daemon = True
        t.start()

    def __process_message(self):
        # type: () -> None
        """Block for message then dispatch it to the proper method"""
        try:
            message = self.__queue.get(True, 120)
            if message == "activate":
                self.__activate_user()

            if self.__user.active:
                message = message.lower()
                self.__lookup_action(message)
                if self.__open_question:
                    self.__process_message()

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
        # type: () -> None
        """Activate the user associated with this session"""
        self.__user.active = True
        self.__user.save()
        self.__send_message("User activated")

    def __lookup_action(self, message):
        # type: (str) -> None
        """Parse a message and dispatch to the proper method"""

        # should probably try to do this without the returns
        # help check
        if message == "help":
            self.__show_help()
            return

        # inactivate check
        if message == "inactivate":
            self.__inactivate_user()
            return

        # get hours check
        if re.search("^(?:get|show) (?:hours|options|settings)$", message):
            self.__show_opts()
            return

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
        # type: (time, time) -> None
        """Set working hours"""
        self.__user.on_time = start
        self.__user.off_time = end
        self.__user.save()
        self.__send_message("Hours set")

    def __inactivate_user(self):
        # type: () -> None
        """Inactivate the user assoc"""
        self.__user.active = False
        self.__user.save()
        self.__send_message("User deactivated")

    def __send_message(self, message):
        # type: (str) -> None
        """Send a pm to the user associated with this session"""
        slack_sock.send_msg(message, channel_id=self.__channel_id, confirm=False)

    def __show_opts(self):
        # type: () -> None
        """Show the user's selected options"""
        self.__send_message("Active\n"
                            "Work hours -- {0} - {1}\n"
                            "Lunch hours -- {2} - {3}".format(self.__user.on_time.strftime("%I:%M %p"),
                                                              self.__user.off_time.strftime("%I:%M %p"),
                                                              self.__user.lunch_on.strftime("%I:%M %p"),
                                                              self.__user.lunch_off.strftime("%I:%M %p")))

    def __show_help(self):
        # type: () -> None
        """Show help blurb"""
        self.__send_message("-- gudbot help --\n"
                            "activate -- activate user\n"
                            "set hours HH(:MM)?(AM|PM)?-HH(:MM)?(AM|PM)? -- set start and stop hours\n"
                            "set lunch hours HH(:MM)?(AM|PM)?-HH(:MM)?(AM|PM)? -- set lunch start and stop hours\n"
                            "get hours -- show work and lunch hours")

    def __set_lunch_hours(self, start, end):
        # type: (time, time) -> None
        """Set user's lunch hours"""
        self.__user.lunch_on = start
        self.__user.lunch_off = end
        self.__user.save()
        self.__send_message("Lunch hours set")


def check_active_tickets():
    Timer(1800, check_active_tickets, ()).start()
    for u in User.select().where(User.active == True):
        in_progress = jira_conn.search_issues("project=ROP and assignee=matt and status=\"In Progress\"")


def main():
    next_half_hour = datetime.now()
    if next_half_hour.minute <= 30:
        next_half_hour.replace(minute=30)
    else:
        next_half_hour += timedelta(hours=1)
        next_half_hour.replace(minute=0)
    Timer((next_half_hour - datetime.now()).total_seconds(), check_active_tickets, ()).start()

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
    try:
        main()
    except KeyboardInterrupt:
        print "Got Ctrl-C shutting down"
        sys.exit()
