# Tacthrift Notecard Satphone design validation

This pass applies the five-step engineering process described by Elon Musk in the Everyday Astronaut Starbase interview. The steps are intentionally used in order.

## 1. Question the requirements

The user's real jobs are: connect the kit, send a message, receive a message, and understand whether the system is ready. Diagnostics, configuration, queue cleanup, firmware, and logs support those jobs; they are not equal-weight destinations.

Success criteria:

- A new user can tell what to do from the first screen.
- Hardware discovery does not require knowing a serial-port name.
- The interface distinguishes device-to-Discord from Discord-to-device messaging.
- Potentially destructive work still requires explicit confirmation.

## 2. Delete

- Deleted the separate Dashboard destination and its duplicate message/repair actions.
- Reduced eight top-level tabs to four: Messages, Device, Tools, Help.
- Removed the four-button “Quick actions” grid.
- Removed blue primary styling from routine, advanced, and destructive buttons.
- Removed setup prose from the main workflow when a short status sentence is enough.

## 3. Simplify and optimize

- Messages is now the default screen.
- One readiness card separates USB detection from honest, read-only satellite status.
- One compact Activity card replaces the ambiguous frozen-looking state with a current step, elapsed time, animated progress, and optional details.
- The app chooses the Notecard USB port automatically; manual port choice remains under Device.
- “Send to Discord” and “Receive from Discord” use short explanations and one clear primary action each.
- Diagnostics and connection settings live under Device. Queue cleanup, firmware, and logs live under Tools.
- Advanced and destructive controls are visually secondary.

## 4. Accelerate the loop

- USB discovery runs at startup and every five seconds, so plugging the device in updates the app without navigation.
- A completed USB task immediately rechecks the physical connection.
- The message byte counter updates while typing.
- The health check opens its detailed result automatically.
- Sends and receives report USB opening, template setup, local queueing, satellite waiting/connection, transfer, and completion as they happen.

## 5. Automate

- Safe automation is limited to USB rediscovery, serialized device access, optional receive-after-`/satphone`, and the existing bounded Discord bridge watchdog.
- The app does not automate message deletion, persistent device changes, or firmware flashing.

## Validation checklist

- Automated tests cover the four-section navigation, default Messages screen, secondary grouping, disconnected-device state, live operation steps, and protection against message-body leakage in activity text.
- The app must be visually checked at its minimum and default window sizes.
- A real-device health check remains required whenever hardware is unavailable during packaging.
