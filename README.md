# HexDriveUseTemplate
Template minimal application to use the HexDrive hexpansion for EMF Camp Tildagon Badge 2024

## General Guidance ##
Please see [Official Tildagon Badge Documentation](https://tildagon.badge.emfcamp.org/tildagon-apps/development/) for information on how to write and install your own apps on the badge.

## Specific Guidance ##
Use this repo as a template (i.e. copy it rather than fork it). Then edit the **metadata.json** files to replace the **"callable"** and **"name"** fields and in the **tildagon.toml** file edit the **name**, **entry** and **url** fields as appropriate for your application.  

### app.py ###
The template app uses settings to define a number of parameters.  It has three states:
* **STATE_INIT** - Scan for HexDrive in all hexpansion slots. 
* **STATE_IDLE** - Wait for **Confirm** button press to start.
* **STATE_RUN_MOTORS** - Drive the Motors via the HexDrive until **Cancel** button pressed.
* **STATE_RUN_SERVOS** - Drive the Servos via the HexDrive until **Cancel** button pressed.


#### Settings ####
Settings are loaded from and saved to the main **settings.json** file using a prefix of the app name which is defined in the **app.py** program by the variable **_APP_NAME**

#### HexDrive Types ####
The HexDrive identifies what hardware is (expected to be) connected to it using its hexpansion PID.  The scan looks at this to find out what type it is from the list in **_HEXDRIVE_TYPES**


