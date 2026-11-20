---
name: script
description: write a small zsh or Python script and try it once
triggers: скрипт, script, автоматизир, automate
---
1. Pick zsh for file and command glue, python3 for text or data work. Standard library only, no pip installs.
2. Keep it under 40 lines. Start with a shebang (#!/bin/zsh or #!/usr/bin/env python3) and one comment line saying what it does.
3. Never delete files in a script; move them to ~/.Trash instead. For bulk renames or moves, print what would change unless the script is called with --apply.
4. Save it with write_file in the scripts folder (it becomes executable by itself).
5. Run it once by its full path (command = the path itself, nothing else) to check it works. If it fails, fix it with write_file and run it again.
6. Tell the user the script path and how to run it. If they want it to repeat, use the routine skill.
