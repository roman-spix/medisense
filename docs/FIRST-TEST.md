# Testing MediSense 0.2 with your board

For the setup you have now: the MacBook, the ESP32-S3 board and the webcam
(no radar or pulse sensor yet). About 40 minutes, including one sitting.

## 1. Download the new version

1. Open [github.com/roman-spix/medisense](https://github.com/roman-spix/medisense),
   click the green **Code** button, then **Download ZIP**.
2. Safari unzips it into your **Downloads** folder (a folder named
   `medisense-main` or similar). If an older `medisense-main` folder is
   still there, delete it first so the two don't get mixed up.

## 2. Install the update

1. If MediSense is running, close its Terminal window.
2. Open the new folder in Downloads. **Right-click** `Install MediSense.command`,
   choose **Open**, then **Open** again (macOS asks because the file came from
   the internet).
3. A Terminal window shows the progress for a few minutes. Look for:
   - `Updating MediSense 0.1.0 → 0.2.0`
   - `✓ posture engine ready (works in the background, even with the page closed)`
   - `MediSense 0.2.0 is installed.`

   If instead it says `! posture engine not installed`, posture still works
   through the browser's camera; send me the lines above it.
4. Your Desktop now has four icons: **MediSense**, **MediSense Demo**,
   **MediSense Posture** and **MediSense Pro** (Pro is for the radar and the
   pulse sensor; ignore it for now).

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
2. Tick **Watch my posture with the webcam**, then **Start session**. The
   first time, macOS asks whether **Terminal** may use the camera: click
   **OK**. (Missed it? System Preferences → Security & Privacy → Camera, tick
   Terminal, then close and restart MediSense.)
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

1. Close MediSense first (its Terminal window): two camera apps at once are
   heavy for an older MacBook.
2. Double-click **MediSense Posture**. Its page opens.
3. Sit well at the computer: back upright, shoulders relaxed, head over your
   shoulders, hands on the keyboard. Click **Calibrate my posture**.
4. Click **Send a test notification**. A notification from "Script Editor"
   should appear. If not: System Preferences → Notifications → Script Editor
   → Allow Notifications.
5. Work as usual. To test it, slump in the chair for half a minute: a
   reminder names what drifted. You can close the browser page; keep the
   Terminal window open (minimised is fine). Closing that window stops it.

## 6. What to send back

- Screenshots: the Live tab during the sitting, the session's detail page,
  the Board tab (Firmware line and Interruptions), and the MediSense Posture
  page.
- Your breath counts next to the app's number, and your Polar readings.
- Any lines in red in the Terminal windows.
