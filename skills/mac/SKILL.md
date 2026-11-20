---
name: mac
description: control the Mac: dark mode, volume, battery, Wi-Fi, apps, speech, screen
triggers: тёмн, темн, светл(ая|ую) тем, громкост, звук, батаре, заряд, wi-?fi, вайфай, открой, скажи вслух, произнеси, экран, dark mode, volume, battery, open app, say aloud
---
Use run with these exact commands (fill in the parts in CAPS):
- Dark mode on/off/toggle: osascript -e 'tell app "System Events" to tell appearance preferences to set dark mode to true' (false to turn off, "not dark mode" to toggle)
- Volume 0-100: osascript -e 'set volume output volume 40'
- Mute: osascript -e 'set volume output muted true'
- Battery: pmset -g batt
- Wi-Fi network name: networksetup -getairportnetwork en0
- Open an app: open -a "APP NAME"
- Open a web page: open "https://..."
- Show a file in Finder: open -R "PATH"
- Speak text aloud: say "TEXT"
- Sleep the display: pmset displaysleepnow
- Keep awake for N minutes: caffeinate -d -t SECONDS (run it in the background with &)
- Disk space: df -h /
- Clipboard text: pbpaste
