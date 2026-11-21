# Changelog

## 1.4.0
- Launcher: a **Play** button starts the companion and launches WoW Forever through Battle.net (`--exec="launch WoWF"`).
  Battle.net is found from the registry or the usual folders; if not, the launcher asks once and remembers it.
- Launcher: optional **Stop the companion when WoW closes** (one window check every ~3 s, after 10 s without WoW).
- Launcher: removed the Quit button (closing the window does the same).
- Widget: tries to let horizontal swipes through to iCUE so you can switch widgets.

## 1.3.0
- First public release.
- `WoWXE.exe`: no Python install needed. Releases are built and tested automatically on GitHub.
- The launcher offers to install or update the addon when it starts.
- The companion also finds the game when it runs in a window that isn't in the corner of the screen.
- Removed debug-only commands. Added the license, README, screenshots and `.gitignore`.

## 1.2.1
- Equipment slots no longer show the item level number, which looked like a stack count. The item level is in the tooltip.

## 1.2.0
- The Gear tab shows the full character sheet: General, Primary Attributes, Weapons, Modifiers and Defense. Buffed values are green.
- Profession icons show reliably.
- Bigger tab buttons, and the pages fill the column.

## 1.1.0
- Info, Gear and Professions tabs. The equipment panel has tap-for-tooltip, and the XP bar hides at max level.
- The launcher minimizes to the tray and stops the companion when closed.
- Navy background matching the stand-by screen.

## 1.0.0
- Renamed to WoWXE, with a new icon.
- Tkinter launcher with a config file, automatic monitor detection, the WoW folder check and tray support.

## Earlier (as "WoW HUD")
- Pixel-strip protocol, reading map and icon pictures from the game files, complete zone maps from the Warcraft Wiki,
  item tooltips, the stand-by screen, the messages button and version display.
