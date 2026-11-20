---
name: files
description: find files, see what takes space, sum up a document
triggers: найди файл, где лежит, сколько места, занимает, перескажи, кратко, суммари, summarize, find file, disk space, what is in
---
- Find by name anywhere: mdfind -name "PART OF NAME" | head -20
- Find by content (Spotlight): mdfind "WORDS" | head -20
- Biggest items in a folder: du -sh "FOLDER"/* | sort -rh | head -15
- Recent files in a folder: ls -lt "FOLDER" | head -15
- File type and size: file "PATH"; stat -f '%z bytes, changed %Sm' "PATH"
- Text of a .docx, .rtf or .html file: textutil -convert txt -stdout "PATH" | head -200
- PDF text cannot be read here; say so and offer the file name, size and date.
To sum up a long text file: read_file, and if it says there is more, read the next part with offset. Summarize each part in a few lines, then give one short summary at the end.
