from jira import JIRA
from slacksocket import SlackSocket
from threading import Thread, Timer
import queue
from db import *
import re
from datetime import time, datetime, timedelta
import sys
from pytz import timezone
import config
from typing import Dict

slack_sock = SlackSocket(config.slack_token, True, ["message"])
active_sessions = {}  # type: Dict[str, Session]
jira_conn = JIRA(server=config.jira_server, basic_auth=(config.jira_user, config.jira_pass))
check_timer = None  # type: Timer


# TODO: Should possibly refactor
# TODO: Everything here should use slack buttons
class Session(object):
    def __init__(self, username: str, context: Event = None) -> None:
        self.__queue = queue.Queue()
        channel = slack_sock.get_im_channel(username)
        self.__channel_id = channel["id"]
        try:
            rec = User.get(User.username == username)
        except User.DoesNotExist:
            rec = User.create(username=username)
        self.__user = rec
        self.active = False
        self.__context = context
        self.__prev_ticket = None  # type: PrevTicket

        # this should happen elsewhere
        if context:
            self.active = True
            if context.conflict_type == "on_over":
                ticket_dict = {t.key: t for t in jira_conn.search_issues("key in ({0})"
                                                                         .format(",".join(context.tickets_affected)))}

                self.__send_message("\n".join(["You have two or more tickets in progress, which are you currently "
                                               "working on?"] +
                                              ["[{0}] - {1} -- {2}".format(idx + 1, t, ticket_dict[t].fields.summary)
                                               for (idx, t) in enumerate(context.tickets_affected)]))
            elif context.conflict_type == "on_under":
                if self.__user.prev_tickets.count():
                    self.__prev_ticket = self.__user.prev_tickets[0].ticket_key
                    ticket = jira_conn.issue(self.__prev_ticket)
                    self.__send_message("You have no tickets in progress.\n"
                                        "If you want move {0} - {1} to 'In Progress' reply with 'yes'.\n"
                                        "Reply with 'no' to dismiss this message.".format(self.__prev_ticket,
                                                                                          ticket.fields.summary))
                else:
                    self.__send_message("You have no tickets 'In Progress'.\n"
                                        "Reply with 'resolve' to dismiss this message.")

            elif context.conflict_type == "off_over":
                self.__send_message("You have one or more tickets 'In Progress'.\n"
                                    "If you would like to move them to 'On Hold', reply with 'yes'.\n"
                                    "Reply with 'no' to dismiss this message")

    def queue_message(self, message: str) -> None:
        """Queue a message for processing"""
        self.__queue.put(message)

    def start_worker(self) -> None:
        """Start worker thread"""
        self.active = True
        t = Thread(target=self.__process_message)
        t.daemon = True
        t.start()

    def __process_message(self) -> None:
        """Block for message then dispatch it to the proper method"""
        try:
            message = self.__queue.get(True, 1800)
            if message == "activate":
                self.__activate_user()

            if not self.__user.active:
                self.__send_message("Your user appears to be inactive, you have either disabled it or have not "
                                    "initialized it.  If you just want to activate it with the current settings"
                                    "(defaults are 9-5 lunch 12-1), reply with 'activate'.  To learn how to set hours,"
                                    " reply with 'help'.")
            message = message.lower()
            self.__lookup_action(message)
            if self.__context and self.__context.active:
                self.__process_message()

        except queue.Empty:
            if self.__context and self.__context.active:
                slack_sock.send_msg("Time's up, you'll need to resolve this event via JIRA.",
                                    channel_id=self.__channel_id)
        finally:
            self.active = False

    def __activate_user(self) -> None:
        """Activate the user associated with this session"""
        self.__user.active = True
        self.__user.save()
        self.__send_message("User activated")

    def __lookup_action(self, message: str) -> None:
        """Parse a message and dispatch to the proper method"""

        # TODO: Should do this better
        if self.__context:
            if self.__context.conflict_type == "on_over":
                if re.search("^\d+$", message):
                    chosen = None
                    for idx, t_key in enumerate(self.__context.tickets_affected):
                        if idx == int(message) - 1:
                            chosen = t_key
                        else:
                            ticket = jira_conn.issue(t_key)
                            transition = jira_conn.find_transitionid_by_name(ticket, "Halt Work")
                            jira_conn.transition_issue(ticket, transition)
                    self.__send_message("All 'In Progress' tickets other than {0} have been set to 'On Hold'."
                                        .format(chosen))
                    self.__context.active = False
                    self.__context.save()
            elif self.__context.conflict_type == "on_under":
                if message == "yes" and self.__prev_ticket:
                    ticket = jira_conn.issue(self.__prev_ticket)
                    transition = jira_conn.find_transitionid_by_name(ticket, "Resume Work")
                    self.__send_message("{0} has been set to 'In Progress'.".format(self.__prev_ticket))
                    jira_conn.transition_issue(ticket, transition)
                elif message == "no" or message == "resolve":
                    self.__send_message("Event resolved.")
                    self.__context.active = False
                    self.__context.save()
            elif self.__context.conflict_type == "off_over":
                if message == "yes":
                    for t_key in self.__context.tickets_affected:
                        ticket = jira_conn.issue(t_key)
                        transition = jira_conn.find_transitionid_by_name(ticket, "Halt Work")
                        jira_conn.transition_issue(ticket, transition)
                    self.__send_message("All 'In Progress' tickets have been set to 'On Hold'.")
                    self.__context.active = False
                    self.__context.save()
                elif message == "no":
                    self.__send_message("Event resolved.")
                    self.__context.active = False
                    self.__context.save()

        # help check
        if message == "help":
            self.__show_help()

        # inactivate check
        elif message == "inactivate":
            self.__inactivate_user()

        # pause check
        elif message == "pause":
            self.__pause_ticket()

        # resume check
        elif message == "resume":
            self.__resume_ticket()

        # get hours check
        elif re.search("^(?:get|show) (?:hours|options|settings)$", message):
            self.__show_opts()

        # get team check
        elif re.search("^(?:get|show) (?:people|team)$", message):
            self.__show_people()

        # set hours check
        match = re.search("^set hours (\d{1,2})(?::(\d{1,2}))? ?(am|pm)? ?- ?(\d+)(?::(\d{1,2}))? ?(am|pm)?$", message)
        if match:
            self.__set_hours(time(hour=int(match.group(1)) if match.group(3) != "pm" else int(match.group(1)) + 12,
                                  minute=int(match.group(2)) if match.group(2) else 0),
                             time(hour=int(match.group(4)) + 12 if match.group(6) != "am" else int(match.group(4)),
                                  minute=int(match.group(5)) if match.group(5) else 0))

        # set lunch hours check
        match = re.search("^set lunch hours (\d{1,2})(?::(\d{1,2}))? ?(am|pm)? ?- ?(\d+)(?::(\d{1,2}))? ?(am|pm)?$",
                          message)
        if match:
            self.__set_lunch_hours(time(hour=int(match.group(1)) if match.group(3) != "pm"
                                        else int(match.group(1)) + 12,
                                        minute=int(match.group(2)) if match.group(2) else 0),
                                   time(hour=int(match.group(4)) + 12 if match.group(6) != "am"
                                        else int(match.group(4)),
                                        minute=int(match.group(5)) if match.group(5) else 0))

    def __set_hours(self, start: time, end: time) -> None:
        """Set working hours"""
        self.__user.on_time = start
        self.__user.off_time = end
        self.__user.save()
        self.__send_message("Hours set")

    def __inactivate_user(self) -> None:
        """Inactivate the user assoc"""
        self.__user.active = False
        self.__user.save()
        self.__send_message("User deactivated")

    def __send_message(self, message: str) -> None:
        """Send a pm to the user associated with this session"""
        slack_sock.send_msg(message, channel_id=self.__channel_id, confirm=False)

    def __show_opts(self) -> None:
        """Show the user's selected options"""
        self.__send_message("{4}\n"
                            "Work hours -- {0} - {1}\n"
                            "Lunch hours -- {2} - {3}".format(self.__user.on_time.strftime("%I:%M %p"),
                                                              self.__user.off_time.strftime("%I:%M %p"),
                                                              self.__user.lunch_on.strftime("%I:%M %p"),
                                                              self.__user.lunch_off.strftime("%I:%M %p"),
                                                              "Active" if self.__user.active else "Inactive"))

    def __show_help(self) -> None:
        """Show help blurb"""
        if self.__context:
            if self.__context.conflict_type == "on_over":
                self.__send_message("-- gudbot help --\n"
                                    "- Overworked edition -\n"
                                    "1-N -- Choose that ticket as the one you are currently working on, all others will"
                                    " be set to hold.\n"
                                    "DO NOTHING -- This message will not display again while unresolved.  After 30 "
                                    "minutes, you will not be able to resolve this event via this chat.  It will "
                                    "automagically resolve itself once you resolve it manually via JIRA.")

            elif self.__context.conflict_type == "on_under":
                self.__send_message("-- gudbot help --\n"
                                    "- Forgetful edition -\n"
                                    "yes -- (may not be available) Move {0} back to 'In Progress'.\n"
                                    "no -- Resolve event, this will trigger again in 5 minutes if you don't have a "
                                    "ticket 'In Progress' then.\n"
                                    "resolve -- Alias for no\n"
                                    "DO NOTHING -- This message will not display again while unresolved.  After 30 "
                                    "minutes, you will not be able to resolve this event via this chat.  It will "
                                    "automagically resolve itself once you resolve it manually via JIRA."
                                    .format(self.__prev_ticket if self.__prev_ticket else "your previous ticket"))

            elif self.__context.conflict_type == "off_over":
                self.__send_message("-- gudbot help --\n"
                                    "- There's no escape edition -\n"
                                    "yes -- Move {0} to 'On Hold'\n"
                                    "no -- Resolve event, this will trigger again in 5 minutes if you still have any "
                                    "tickets 'In Progress' then.\n"
                                    "DO NOTHING -- This message will not display again while unresolved.  After 30 "
                                    "minutes, you will not be able to resolve this event via this chat.  It will "
                                    "automagically resolve itself once you resolve it manually via JIRA."
                                    .format(", ".join(self.__context.tickets_affected)))
        else:
            self.__send_message("-- gudbot help --\n"
                                "activate -- activate user\n"
                                "inactivate -- inactivate user(you will no longer receive notifications)\n"
                                "set hours HH(:MM)?(AM|PM)?-HH(:MM)?(AM|PM)? -- set start and stop hours\n"
                                "set lunch hours HH(:MM)?(AM|PM)?-HH(:MM)?(AM|PM)? -- set lunch start and stop hours\n"
                                "get hours -- show work and lunch hours\n"
                                "pause -- set current case(s) to 'On Hold'\n"
                                "resume -- set last case to 'In Progress'\n"
                                "get team -- show the people in your team\n"
                                "Wondering about another part of this bot?  'help' changes depending on the context")

    def __set_lunch_hours(self, start: time, end: time) -> None:
        """Set user's lunch hours"""
        self.__user.lunch_on = start
        self.__user.lunch_off = end
        self.__user.save()
        self.__send_message("Lunch hours set")

    def __pause_ticket(self) -> None:
        in_progress = jira_conn.search_issues("project={0} and assignee={1} and status=\"In Progress\""
                                              .format(config.jira_project, self.__user.username))
        for ticket in in_progress:
            transition = jira_conn.find_transitionid_by_name(ticket, "Halt Work")
            jira_conn.transition_issue(ticket, transition)
        if in_progress.total > 0:
            if self.__user.prev_tickets.count():
                prev_ticket = self.__user.prev_tickets  # type: PrevTicket
                prev_ticket.ticket_key = in_progress[0].key
                prev_ticket.save()
            else:
                PrevTicket.create(user=self.__user, ticket_key=in_progress[0].key)
            if in_progress.total == 1:
                self.__send_message("{0} has been set to 'On Hold'.".format(in_progress[0].key))
            else:
                self.__send_message("All tickets have been set to 'On Hold'.")
        else:
            self.__send_message("You have no in progress tickets.")

        self.__resolve_all()

    def __resume_ticket(self) -> None:
        in_progress = jira_conn.search_issues("project={0} and assignee={1} and status=\"In Progress\""
                                              .format(config.jira_project, self.__user.username))
        for ticket in in_progress:
            transition = jira_conn.find_transitionid_by_name(ticket, "Halt Work")
            jira_conn.transition_issue(ticket, transition)

        self.__resolve_all()

        if self.__user.prev_tickets.count():
            ticket = jira_conn.issue(self.__user.prev_tickets[0].ticket_key)
            transition = jira_conn.find_transitionid_by_name(ticket, "Resume Work")
            jira_conn.transition_issue(ticket, transition)
            self.__send_message("{0} has been set to 'In Progress'.".format(ticket.key))
        else:
            self.__send_message("You have no previous ticket.")

    def __show_people(self) -> None:
        buffer_list = ["Users in your team:"]
        for u in User.select():
            buffer_list.append("{0} -- {1} - {2}".format(u.username, u.on_time.strftime("%I:%M %p"),
                                                         u.off_time.strftime("%I:%M %p")))
        self.__send_message("\n".join(buffer_list))

    def resolve_event(self) -> None:
        self.__context.active = False
        self.__context.save()
        self.active = False

    def __resolve_all(self) -> None:
        for e in self.__user.events.where(Event.active == True):
            e.active = False
            e.save()


def check_active_tickets() -> None:
    global check_timer
    check_timer = Timer(300, check_active_tickets, ())
    check_timer.start()
    if datetime.now(timezone(config.time_zone)).weekday() == 5 or \
            datetime.now(timezone(config.time_zone)).weekday() == 6:
        return
    for u in User.select().where(User.active == True):
        # TODO: Fix for multiple projects
        in_progress = jira_conn.search_issues("project={0} and assignee={1} and status=\"In Progress\""
                                              .format(config.jira_project, u.username))

        start_time = datetime.combine(datetime.now(timezone(config.time_zone)).date(), u.on_time)\
            .replace(tzinfo=timezone(config.time_zone))
        off_time = datetime.combine(datetime.now(timezone(config.time_zone)).date(), u.off_time)\
            .replace(tzinfo=timezone(config.time_zone))
        lunch_start_time = datetime.combine(datetime.now(timezone(config.time_zone)).date(), u.lunch_on)\
            .replace(tzinfo=timezone(config.time_zone))
        lunch_stop_time = datetime.combine(datetime.now(timezone(config.time_zone)).date(), u.lunch_off)\
            .replace(tzinfo=timezone(config.time_zone))

        # check if it's lunchtime
        if lunch_start_time <= datetime.now(timezone(config.time_zone)) <= lunch_stop_time:
            if in_progress.total > 1:
                ticket_keys = [t.key for t in in_progress]
                if not any(set(e.ticket_list) == set(ticket_keys) for e in
                           u.events.where((Event.active == True) & (Event.conflict_type == "on_over"))):
                    for e in u.events.where(Event.active == True):
                        e.active = False
                        e.save()
                    context = Event.create(conflict_type="on_over", user=u)
                    context.tickets_affected = ticket_keys
                    context.save()
                    s = Session(u.username, context)
                    active_sessions[u.username] = s
                    s.start_worker()
        else:
            # check if in work hours with one hour of grace
            if (start_time + timedelta(hours=1)) <= datetime.now(timezone(config.time_zone)) <= \
                    (off_time - timedelta(hours=1)):
                if in_progress.total > 1:
                    ticket_keys = [t.key for t in in_progress]
                    if not any(set(e.tickets_affected) == set(ticket_keys) for e in
                               u.events.where((Event.active == True) & (Event.conflict_type == "on_over"))):
                        for e in u.events.where(Event.active == True):
                            e.active = False
                            e.save()
                        context = Event.create(conflict_type="on_over", user=u)
                        context.tickets_affected = ticket_keys
                        context.save()
                        s = Session(u.username, context)
                        active_sessions[u.username] = s
                        s.start_worker()

                elif in_progress.total == 0:
                    if not u.events.where((Event.active == True) & (Event.conflict_type == "on_under")):
                        for e in u.events.where(Event.active == True):
                            e.active = False
                            e.save()
                        context = Event.create(conflict_type="on_under", user=u)
                        s = Session(u.username, context)
                        active_sessions[u.username] = s
                        s.start_worker()

                # recording last worked on ticket for suggestion
                else:
                    if u.prev_tickets.count():
                        prev_ticket = u.prev_tickets  # type: PrevTicket
                        prev_ticket.ticket_key = in_progress[0].key
                        prev_ticket.save()
                    else:
                        PrevTicket.create(user=u, ticket_key=in_progress[0].key)

                    for e in u.events.where(Event.active == True):
                        e.active = False
                        e.save()

            # check if not in work hours with one hour of grace
            elif not ((start_time - timedelta(hours=1)) <= datetime.now(timezone(config.time_zone)) <=
                      (off_time + timedelta(hours=1))):
                if in_progress.total > 0:
                    for e in u.events.where(Event.active == True):
                        e.active = False
                        e.save()
                    context = Event(conflict_type="off_over", user=u)
                    context.tickets_affected = [t.key for t in in_progress]
                    context.save()
                    s = Session(u.username, context)
                    active_sessions[u.username] = s
                    s.start_worker()

                else:
                    for e in u.events.where(Event.active == True):
                        e.active = False
                        e.save()


def main() -> None:
    next_five = datetime.now()
    next_five = next_five.replace(minute=(next_five.minute + (5 - (next_five.minute % 5))))
    global check_timer
    check_timer = Timer((next_five - datetime.now()).total_seconds(), check_active_tickets, ())
    check_timer.start()

    while True:
        event = slack_sock.get_event().event
        if not ("hidden" in event and event["hidden"]) and event["user"] == event["channel"] and \
                event["user"] != "slackbot":
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
        print("Got Ctrl-C shutting down")
        if check_timer:
            check_timer.cancel()
        sys.exit()
