# herdr-link-opener

Herdr opens only `http://` and `https://` links on Ctrl-click; this plugin also opens other schemes, such as `vscode://`, on the computer where you run the Herdr TUI.
It works in local panes and in panes on saved SSH machines: the cluster appends the clicked link to a queue, and a listener on your computer reads that queue over SSH and opens the link.
Use it to open `vscode://` file links that agents print on the cluster in your local editor.

## Setup

Requirements: Herdr 0.9.1 or later and Python 3.8 or later on both machines, and the cluster saved as a Herdr machine that you can SSH to without a password prompt.

On your computer (the one with the desktop):

```bash
herdr plugin install bhoov/herdr-link-opener
herdr plugin action invoke bhoov.link-opener.restart   # start the listener now; later it starts with Herdr
```

On the cluster:

```bash
herdr plugin install bhoov/herdr-link-opener
```

## Add a scheme

Edit `schemes` in the config on your computer, then restart the listener:

```bash
echo '{"schemes": ["vscode", "vscode-insiders", "zed"]}' > "$(herdr plugin config-dir bhoov.link-opener)/config.json"
herdr plugin action invoke bhoov.link-opener.restart
```
