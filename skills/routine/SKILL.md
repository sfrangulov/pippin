---
name: routine
description: repeat a job on a schedule (every day at 9:00, every 30 minutes)
triggers: кажд(ый|ое|ую|ые|ой|ого), ежедневн, по (утрам|вечерам|будням|выходным|понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям), расписани, рутин, every (day|morning|evening|night|hour|week|weekday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d), daily, weekdays, schedule, routine
tools: routine
shadows: remind
---
To make something repeat:
1. If the job is more than one short command, first write a script with write_file into the scripts folder, then schedule `zsh <full script path>`.
2. Call routine with action=add, a short name (like drink-water), and either at="HH:MM" or every_minutes. Weekdays in the request (weekdays, Mondays) are picked up by themselves.
3. For a repeating popup, pass text (the message) and no command. Otherwise pass command.
4. Tell the user the name and when it runs. Output goes to ~/.pippin/logs/<name>.log.
To see jobs: routine action=list. To stop one: routine action=remove with its name.
