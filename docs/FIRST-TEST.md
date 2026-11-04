# Testing MediSense 0.2.1 with your board

For the setup you have now: the MacBook, the ESP32-S3 board and the webcam
(no radar or pulse sensor yet). About 40 minutes, including one sitting.

## 1. Remove the old version (a clean start)

1. Close every MediSense Terminal window, and every MediSense tab in your
   browser.
2. Download the new version: open
   [github.com/roman-spix/medisense](https://github.com/roman-spix/medisense),
   click the green **Code** button, then **Download ZIP**. Safari unzips it
   into **Downloads**. Delete older `medisense-main` folders there first.
3. In the new folder, **right-click** `Uninstall MediSense.command`, choose
   **Open**, then **Open** again. It removes the old program, its Python and
   the Desktop icons. When it asks about your recordings, just press
   **Return** to keep them.

   (Without the folder, the same in Terminal:
   `rm -rf ~/Applications/MediSense ~/.medisense ~/MediSense/cache; rm -f ~/Desktop/MediSense*.command`)

## 2. Install

1. In the same downloaded folder, **right-click** `Install MediSense.command`,
   choose **Open**, then **Open** again.
2. A Terminal window shows the progress for a few minutes, ending with
   `MediSense 0.2.1 is installed.`
3. Your Desktop now has four icons: **MediSense**, **MediSense Demo**,
   **MediSense Posture** and **MediSense Pro** (Pro is for the radar and the
   pulse sensor; ignore it for now).
4. Quick check: double-click **MediSense Demo**. Within a few seconds the
   badge at the top says **Live · demo**. If a page ever stays at
   "Starting…", press **⌘⇧R** once to reload it.

## 3. Update the board

There is no separate installer for the board: MediSense updates it.

1. Double-click **MediSense** on the Desktop. Your browser opens it.
2. Plug the board into the Mac with its USB cable.
3. Within a few seconds the page says *Updating the board to MediSense
   firmware 0.8.8 + MediSense 2…*. It takes about a minute. Don't unplug it.
   - If macOS asks whether a program may use the "MediSense WiFi" item in
     your keychain, click **Always Allow**.
   - If the page asks for your WiFi password instead, type it once.
4. When the badge at the top says **Live**, open the **Board** tab. The
   **Firmware** line should read *RuView 0.8.8 + MediSense 2*.
5. On the **Live** tab, the **Signal** card shows frames per second. With the
   new firmware it should be higher and steadier than before (35 or more is
   good).

## 4. One test sitting (15–20 minutes)

1. Put the board where it was (about 1 m in front of you, the router behind
   you). Leave it plugged into the Mac, so the Board tab can explain any
   interruption.
2. Tick **Watch my posture with the webcam**, then **Start session**. Your
   browser asks for the camera: click **Allow**. The camera works inside the
   page, as in the first version, so keep the page open during the sitting.
3. Sit in your meditation posture and click **Calibrate** on the Posture card
   (hold still for 5 seconds). Open **The seven-point posture of Vairocana**
   on that card to see each point as Lama Yonten Gyaltso describes it.
4. Meditate. If you like, test it along the way:
   - after about 5 minutes, click **60 s breath count**, count your breaths,
     and compare with the app's number;
   - press **H** and type your Polar's heart rate now and then;
   - once, shift your position; near the end, stand up for 10 seconds and sit
     down again.
5. What to watch: the **Practice** card (*Settling*, then *Steady* after a
   minute without moving, *Restless* when you move), the **Breathing**
   number, and the **Signal** card (*Paused* while the board reconnects).
6. **End session**, then look at the **History** tab: *Settled in*, *Steady*
   and the timeline. *Where you are now* appears after three sessions.

## 5. MediSense Posture at your desk

1. Close MediSense first (its Terminal window and page): two camera pages at
   once are heavy for an older MacBook.
2. Double-click **MediSense Posture**. Its page opens and asks for the
   camera: click **Allow**. Chrome works best for this page.
3. Sit well at the computer: back upright, shoulders relaxed, head over your
   shoulders, hands on the keyboard. Click **Calibrate my posture**.
4. Click **Send a test notification**. A notification from "Script Editor"
   should appear. If not: System Preferences → Notifications → Script Editor
   → Allow Notifications.
5. Work as usual. To test it, slump in the chair for half a minute: a
   reminder names what drifted. **Keep the page open**: it can sit behind
   your other windows (it then checks about once a second), but closing it
   stops the camera. Keep the Terminal window open too (minimised is fine).

## 6. What to send back

- Screenshots: the Live tab during the sitting, the session's detail page,
  the Board tab (Firmware line and Interruptions), and the MediSense Posture
  page.
- Your breath counts next to the app's number, and your Polar readings.
- Any lines in red in the Terminal windows.
