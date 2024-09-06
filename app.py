import asyncio
import time
import settings
from app_components.tokens import label_font_size, clear_background, button_labels
from events.input import BUTTON_TYPES, Buttons
from machine import I2C
from system.eventbus import eventbus
from system.hexpansion.events import (HexpansionInsertionEvent,
                                      HexpansionRemovalEvent)
from system.scheduler import scheduler
from system.hexpansion.util import read_hexpansion_header, detect_eeprom_addr

import app


_APP_NAME = "myapp" # change this to the name of your app - used as a prefix for settings

# Motor Driver - Defaults
_MAX_POWER = 65535
_POWER_STEP_PER_TICK = 7500  # effectively the acceleration, based on 10ms ticks
_PWM_FREQ = 10000

# Servo Control Parameters
_MIN_SERVO_POSITION = -1000 # -1000us to +1000us is the range of the servos (about the centre position of 1500us)
_MAX_SERVO_POSITION = 1000
_SERVO_STEP         = 100   # 100us per step

# App states
STATE_INIT        = -1
STATE_IDLE        =  0
STATE_RUN_MOTORS  =  1
STATE_RUN_SERVOS  =  2

# App states where user can minimise app
MINIMISE_VALID_STATES = [0]


class myHexDriveApp(app.App):
    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)

        # UI Feature Controls
        self.text = ["No hexpansion found."]        

        # Settings
        self._settings = {}
        self._settings['acceleration']  = MySetting(self._settings, _POWER_STEP_PER_TICK, 1, 65535)
        self._settings['max_power']     = MySetting(self._settings, _MAX_POWER,        1000, 65535)
        self._settings['pwm_freq']      = MySetting(self._settings, _PWM_FREQ,           10, 20000)
        self.update_settings()   

        # Hexpansion related
        self._HEXDRIVE_TYPES = [HexDriveType(0xCBCB, motors=2, servos=4), 
                                HexDriveType(0xCBCA, motors=2, name="2 Motor"), 
                                HexDriveType(0xCBCC, servos=4, name="4 Servo"), 
                                HexDriveType(0xCBCD, motors=1, servos=2, name = "1 Mot 2 Srvo")] 
        self.hexdrive_type = None
        self.hexdrive_power = False         
        self.motor_target_output = None          # the output power we are aiming for each motor
        self._motor_current_output = (0,0)       # the current output power for each motor - updated in background task DO NOT MODIFY DIRECTLY
        self.servo_target_position = None        # the target position we are aiming for each servo
        self._servo_current_position = (0,0,0,0) # the current position for each servo - updated in background task DO NOT MODIFY DIRECTLY

        # UI variables
        self.servo_selected = 0

        # Subscribe to Hexpansion events
        eventbus.on_async(HexpansionInsertionEvent, self.handle_hexpansion_insertion, self)
        eventbus.on_async(HexpansionRemovalEvent, self.handle_hexpansion_removal, self)

        # Overall app state (controls what is displayed and what user inputs are accepted)
        self.current_state = STATE_INIT

     
        
    ### ASYNC EVENT HANDLERS ###

    async def handle_hexpansion_removal(self, event: HexpansionRemovalEvent):
        self.hexdrive_app = self.scan_for_hexpansion()        

    async def handle_hexpansion_insertion(self, event: HexpansionInsertionEvent):
        self.hexdrive_app = self.scan_for_hexpansion()        

    async def background_task(self):
        # Modifed background task loop for shorter sleep time
        last_time = time.ticks_ms()
        while True:
            cur_time = time.ticks_ms()
            delta_ticks = time.ticks_diff(cur_time, last_time)
            self.background_update(delta_ticks)
            # Sleep for 10ms if in Running State(s), otherwise sleep for 50ms
            s = 10 if self.current_state in [STATE_RUN_MOTORS] else 50
            await asyncio.sleep_ms(s)
            last_time = cur_time



    ### NON-ASYNC FUCNTIONS ###

    def background_update(self, delta):
        if self.current_state in [STATE_RUN_MOTORS] or any(self._motor_current_output):
            # update power level towards the target_output for each motor
            # limiting change according to the acceleration setting
            # to avoid sudden changes in ouput which the badge may not be 
            # able to supply enough current for.
            new_output = [0]*self._HEXDRIVE_TYPES[self.hexdrive_type].motors
            if self.hexdrive_power:
                for i in range(self._HEXDRIVE_TYPES[self.hexdrive_type].motors):
                    if self.motor_target_output is not None:
                        target_output = self.motor_target_output[i]
                    else:
                        target_output = 0
                    if self._motor_current_output[i] < target_output:
                        new_output[i] = min(self._motor_current_output[i] + self._settings['acceleration'].v, target_output)
                    elif self._motor_current_output[i] > target_output:
                        new_output[i] = max(self._motor_current_output[i] - self._settings['acceleration'].v, target_output)
                    else:
                        new_output[i] = target_output
            self._motor_current_output = tuple(new_output)
            if self.hexdrive_app is not None:
                # update the HexDrive
                self.hexdrive_app.set_motors(self._motor_current_output)

        if self.current_state in [STATE_RUN_SERVOS] or any(self._servo_current_position):
            # update position towards the target_position for each servo
            # limiting change according to the acceleration setting
            # to avoid sudden changes in ouput which the badge may not be 
            # able to supply enough current for.
            new_position = [0]*self._HEXDRIVE_TYPES[self.hexdrive_type].servos
            for i in range(self._HEXDRIVE_TYPES[self.hexdrive_type].servos):
                if self.servo_target_position is not None:
                    target_position = self.servo_target_position[i]
                else:
                    target_position = 0
                # for simplicity we reuse the motor acceleration setting for the servos but scale it down by 100    
                if self._servo_current_position[i] < target_position:
                    new_position[i] = min(self._servo_current_position[i] + (self._settings['acceleration'].v//100), target_position)
                elif self._servo_current_position[i] > target_position:
                    new_position[i] = max(self._servo_current_position[i] - (self._settings['acceleration'].v//100), target_position)
                else:
                    new_position[i] = target_position
                if self.hexdrive_app is not None:
                    # update the HexDrive
                    self.hexdrive_app.set_servoposition(i, new_position[i])                    
            self._servo_current_position = tuple(new_position)



    def hexdrive_initialise_motors(self):
        # Check that we have a HexDrive app to control
        if self.hexdrive_app is None:
            print("No HexDrive app found")
            return
        
        # Check that the HexDrive has motors
        if self._HEXDRIVE_TYPES[self.hexdrive_type].motors == 0:
            return
        
        # Check that the HexDrive has been able to get the PWM resources required
        if not self.hexdrive_app.get_status():
            print("HexDrive PWM resources not availble")
            return
        
        # Set the Motor Drive target values to 0
        self.motor_target_output = (0,0)

        # Set the PWM Frequency to 10kHz (for all channels)
        print("Setting PWM Frequency to " + str(self._settings['pwm_freq'].v))
        self.hexdrive_app.set_freq(self._settings['pwm_freq'].v)
        
        # Turn On the HexDrive Boost Converter
        print("Turning on HexDrive Boost Converter")
        self.hexdrive_app.set_power(True)
        self.hexdrive_power = True


    def hexdrive_initialise_servos(self):
        # Check that we have a HexDrive app to control
        if self.hexdrive_app is None:
            print("No HexDrive app found")
            return
        
        # Check that the HexDrive has servos
        if self._HEXDRIVE_TYPES[self.hexdrive_type].servos == 0:
            return
        
        # Check that the HexDrive has been able to get the PWM resources required
        if not self.hexdrive_app.get_status():
            print("HexDrive PWM resources not availble")
            return
        
        # Set the Servo target position values to 0
        self.servo_target_position = [0,0,0,0]
      
        # Turn On the HexDrive Boost Converter
        print("Turning on HexDrive Boost Converter")
        self.hexdrive_app.set_power(True)
        self.hexdrive_power = True


    def hexdrive_shutdown(self):
        # Check that we have a HexDrive app to control
        if self.hexdrive_app is None:
            print("No HexDrive app found")
            return

        # Set the Motor Drive target values to 0
        self.motor_target_output = (0,0)

        # Set the Servo target position values to 0
        self.servo_target_output = (0,0,0,0)

        # Turn off the Servos
        if self._HEXDRIVE_TYPES[self.hexdrive_type].servos > 0:
            print("Turning off HexDrive Servos")
            self.hexdrive_app.set_servoposition()   # Set all servos to Off

        # Turn Off the HexDrive Boost Converter
        print("Turning off HexDrive Boost Converter")
        self.hexdrive_app.set_power(False)
        self.hexdrive_power = False



    def update_settings(self):
        for s in self._settings:
            self._settings[s].v = settings.get(f"{_APP_NAME}.{s}", self._settings[s].d)


    def scan_for_hexpansion(self) -> app:
        for port in range(1, 7):
            # Searching for hexpansion on port: {port}
            i2c = I2C(port)
            addr,addr_len = detect_eeprom_addr(i2c) # Firmware version 1.8 and upwards only!

            if addr is None:
                continue
            else:
                print(f"Found EEPROM at addr {hex(addr)} on port {port}")

            header = read_hexpansion_header(i2c, addr)
            if header is None:
                continue
            else:
                print("Read header: " + str(header))

            # Check if the header matches any of the known HexDrive types
            for index, hexpansion_type in enumerate(self._HEXDRIVE_TYPES):
                if header.vid == hexpansion_type.vid and header.pid == hexpansion_type.pid:
                    print(f"HexDrive found on port {port}")                    
                    hexdrive_app = self.find_hexdrive_app(port)    
                    if hexdrive_app is None:
                        print(f"No running HexDrive app found on port {port}")                    
                    else:
                        print(f"Found running HexDrive app on port {port}")
                        self.text = [f"Found '{hexpansion_type.name}'",f"HexDrive on port {port}"]
                        self.hexdrive_type = index
                        return hexdrive_app
    

    # Find the running hexdrive app so that we can control it    
    def find_hexdrive_app(self, port) -> app:                    
        for an_app in scheduler.apps:
            if hasattr(an_app, "config") and hasattr(an_app.config, "port") and  an_app.config.port == port:
                return an_app
        return None
    

    ### MAIN APP CONTROL FUNCTIONS ###

    def update(self, delta):
        if self.current_state in MINIMISE_VALID_STATES and self.button_states.get(BUTTON_TYPES["CANCEL"]):
            # Minimise the app
            self.button_states.clear()
            self.minimise()
        elif self.current_state == STATE_INIT:
            self.hexdrive_app = self.scan_for_hexpansion()
            self.current_state = STATE_IDLE
        elif self.current_state == STATE_IDLE:
            if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
                self.button_states.clear()
                if 0 < self._HEXDRIVE_TYPES[self.hexdrive_type].motors:
                    # Start the motors
                    self.hexdrive_initialise_motors()
                    self.current_state = STATE_RUN_MOTORS
                elif 0 < self._HEXDRIVE_TYPES[self.hexdrive_type].servos:
                    # Start the servos
                    self.hexdrive_initialise_servos()
                    self.current_state = STATE_RUN_SERVOS
        elif self.current_state == STATE_RUN_MOTORS:
            if self.button_states.get(BUTTON_TYPES["CANCEL"]):
                self.button_states.clear()
                # Stop the motors
                self.hexdrive_shutdown()
                self.current_state = STATE_IDLE
            # Very simple control using full power in each direction as the target output
            # the background task will limit the change in output to the acceleration setting 
            elif self.button_states.get(BUTTON_TYPES["UP"]):
                self.button_states.clear()
                print("Forward")
                self.motor_target_output = (self._settings['max_power'].v, self._settings['max_power'].v)
            elif self.button_states.get(BUTTON_TYPES["DOWN"]):
                self.button_states.clear()
                print("Reverse")
                self.motor_target_output = (-self._settings['max_power'].v, -self._settings['max_power'].v)
            elif self.button_states.get(BUTTON_TYPES["LEFT"]):
                self.button_states.clear()
                print("Left")
                self.motor_target_output = (-self._settings['max_power'].v, self._settings['max_power'].v)
            elif self.button_states.get(BUTTON_TYPES["RIGHT"]):
                self.button_states.clear()
                print("Right")
                self.motor_target_output = (self._settings['max_power'].v, -self._settings['max_power'].v)
        elif self.current_state == STATE_RUN_SERVOS:
            if self.button_states.get(BUTTON_TYPES["CANCEL"]):
                self.button_states.clear()
                # Stop the servos
                self.hexdrive_shutdown()
                self.current_state = STATE_IDLE
            # Very simple control of selcted servo with buttons
            # the background task will limit the change in position to the acceleration setting 
            elif self.button_states.get(BUTTON_TYPES["UP"]):
                self.button_states.clear()
                print("Next Servo")
                self.servo_selected = (self.servo_selected + 1) % self._HEXDRIVE_TYPES[self.hexdrive_type].servos
            elif self.button_states.get(BUTTON_TYPES["DOWN"]):
                self.button_states.clear()
                print("Prev Servo")
                self.servo_selected = (self.servo_selected - 1) % self._HEXDRIVE_TYPES[self.hexdrive_type].servos
            elif self.button_states.get(BUTTON_TYPES["LEFT"]):
                self.button_states.clear()
                print("Left")
                # Reduce the servo target position by _SERVO_STEP but limit to _MIN_SERVO_POSITION
                self.servo_target_position[self.servo_selected] -= _SERVO_STEP
                if _MIN_SERVO_POSITION > (self.servo_target_position[self.servo_selected]):
                    self.servo_target_position[self.servo_selected] = _MIN_SERVO_POSITION
            elif self.button_states.get(BUTTON_TYPES["RIGHT"]):
                self.button_states.clear()
                print("Right")
                # Increase the servo target position by _SERVO_STEP but limit to _MAX_SERVO_POSITION
                self.servo_target_position[self.servo_selected] += _SERVO_STEP
                if _MAX_SERVO_POSITION < (self.servo_target_position[self.servo_selected]):
                    self.servo_target_position[self.servo_selected] = _MAX_SERVO_POSITION

    def draw(self, ctx):
        clear_background(ctx)           
        ctx.save()
        ctx.text_align = ctx.LEFT
        ctx.text_baseline = ctx.BOTTOM             
        if self.current_state == STATE_IDLE:
            self.draw_message(ctx, self.text, [(1,1,1)], label_font_size)
        elif self.current_state == STATE_RUN_MOTORS:
            # convert current_output to string, scaling values according to the max_power setting to get a value from 0-100
            output_str = str(tuple([int(x/(self._settings['max_power'].v//100)) for x in self._motor_current_output]))
            self.draw_message(ctx, ["Running...",output_str], [(1,1,1),(1,1,0)], label_font_size)
            # draw the button labels for the user to control the motors
            button_labels(ctx, up_label="^", down_label="\u25BC", left_label="<--", right_label="-->",  cancel_label="Stop")
        elif self.current_state == STATE_RUN_SERVOS:
            # convert current_position to string
            output_str = f"{int(self._servo_current_position[self.servo_selected]):+5} "
            self.draw_message(ctx, [f"Running Servo {self.servo_selected+1}",output_str], [(1,1,1),(1,1,0)], label_font_size)
            # draw the button labels for the user to control the motors
            button_labels(ctx, up_label="^", down_label="\u25BC", left_label="<--", right_label="-->",  cancel_label="Stop")            
        ctx.restore()


    def draw_message(self, ctx, message, colours, size):
        ctx.font_size = size
        num_lines = len(message)
        for i_num, instr in enumerate(message):
            text_line = str(instr)
            width = ctx.text_width(text_line)
            try:
                colour = colours[i_num]
            except IndexError:
                colour = None
            if colour is None:
                colour = (1,1,1)
            # Font is not central in the height allocated to it due to space for descenders etc...
            # this is most obvious when there is only one line of text        
            y_position = int(0.35 * ctx.font_size) if num_lines == 1 else int((i_num-((num_lines-2)/2)) * ctx.font_size)
            ctx.rgb(*colour).move_to(-width//2, y_position).text(text_line)


class MySetting:
    def __init__(self, container, default, minimum, maximum):
        self._container = container
        self.d = default
        self.v = default
        self._min = minimum
        self._max = maximum


class HexDriveType:
    def __init__(self, pid, vid=0xCAFE, motors=0, servos=0, name="Unknown"):
        self.vid = vid
        self.pid = pid
        self.name = name
        self.motors = motors
        self.servos = servos

__app_export__ = myHexDriveApp
