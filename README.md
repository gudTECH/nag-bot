# nag-bot
A slack/jira bot to help people stay on task, and keep issues in the right status.

Ask the bot 'help' after activating your user for an explanation of commands.

## Dependencies
* Python >= 3.4
* peewee
* pytz
* jira

## Installation
1. Clone it
2. Copy config-template.py to config.py and fill in the fields
3. Run `python initdb.py` to initialize the SQLite3 DB. 
4. You're good to go `python server.py` will start it up
