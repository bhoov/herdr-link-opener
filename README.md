# herdr-link-opener

A [Herdr](https://herdr.dev) plugin.
Herdr opens only `http://` and `https://` links on Ctrl-click.
With this plugin, Ctrl-click also opens links with other schemes, such as `vscode://`, on the computer where you run the Herdr TUI.
This works in Local panes and in panes on saved SSH machines.

The schemes that open are a list in `config.json`.
The default list is `vscode` and `vscode-insiders`.

Herdr finds a link with another scheme only when the program prints it as an OSC 8 terminal hyperlink.
Herdr does not detect such a link in plain text.

## Setup

Requirements: Herdr 0.9.1 or later and python 3.8 or later on each machine, Linux or macOS.
The computer with the desktop also needs OpenSSH, and SSH access to each saved machine without a password prompt.

Install the plugin on the computer where you run the Herdr TUI, and on each SSH machine whose panes print the links.
On each of them:

```bash
herdr plugin link /path/to/herdr-link-opener
```

On the computer with the desktop, start the listener once.
After this, it starts with the Herdr server.

```bash
herdr plugin action invoke bhoov.link-opener.restart
```

## Configuration

`config.json` is in the directory that `herdr plugin config-dir bhoov.link-opener` prints.
All keys are optional.

```json
{
  "schemes": ["vscode", "vscode-insiders", "zed", "obsidian"],
  "role": "auto",
  "machines": null,
  "opener": null,
  "notify": true
}
```

- `schemes`: the URL schemes that open.
  Only the file on the computer with the desktop counts.
  A link with another scheme shows a toast and does not open.
- `role`: `desktop` opens links, `relay` sends them to the desktop.
  `auto` chooses `relay` inside an SSH session or without a display, and `desktop` otherwise.
- `machines`: labels or ids of the saved machines to listen to.
  `null` means every enabled saved machine.
- `opener`: a command (argv list) that receives the URL as its last argument.
  `null` means `open` on macOS and `xdg-open` on Linux.
- `notify`: show a toast when a link is refused or cannot be delivered.

Run `herdr plugin action invoke bhoov.link-opener.restart` on the desktop after you change the file.

## How it works

Herdr runs a link handler on the Herdr server that owns the clicked pane.
For a pane on an SSH machine, that server has no desktop, so the handler cannot open the link itself.

- On the desktop, the handler gives the link to the operating system.
- On an SSH machine, the handler appends the link to `/tmp/herdr-link-opener-<uid>/queue`.
- A listener on the desktop keeps one SSH connection to each enabled saved machine (`herdr machine list`).
  The connection runs `tail -F` on the queue.
  The listener opens each link that arrives, if its scheme is in `schemes`.

Links clicked while no listener is connected are not opened later.
The handler shows a toast in that case.

## Security

A process that runs as your user on a saved machine can append a link to the queue without a click.
The listener then opens it on your computer.
For this reason:

- only the schemes in `schemes` on the desktop open; keep the list short, and add a scheme only when you accept that the SSH machine can start its application;
- the listener opens at most 5 links per machine in 10 seconds;
- the queue directory has mode 700, and both sides refuse a directory that another user owns;
- the link goes to the opener as one argument, without a shell.

## Commands

```bash
python3 link_opener.py status    # role, schemes, and whether a listener runs or listens
python3 link_opener.py open URL  # the same path as a click
```

The listener log is `daemon.log` in the plugin state directory, `~/.local/state/herdr/plugins/bhoov.link-opener`.
`herdr plugin log list --plugin bhoov.link-opener` lists the handler runs.
