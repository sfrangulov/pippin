---
name: remind
description: one-off reminder or notification (in 20 minutes, right now)
triggers: напомни, напоминалк, уведом, (через|in) \d+ ?(мин|час|min|hour|h\b), через (минуту|час|полчаса), remind, notify me, notification
tools: notify
---
Call notify once with the reminder text, short, in the user's words (for example "Проверить чайник"), and in_minutes from the request (0 = show now).
Then tell the user at what time the reminder will pop up. It does not survive a restart or sleep; for something that repeats, suggest a routine.
