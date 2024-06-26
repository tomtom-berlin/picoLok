#
# "pico Lo" - Digitalsteuerung mit RPI pico
#
# (c) 2024 Thomas Borrmann
# Lizenz: GPLv3 (sh. https://www.gnu.org/licenses/gpl-3.0.html.en)
#
# Funktionen für den Bereich "ELECTRICAL" der NMRA DCC RP 9.2
#
# ----------------------------------------------------------------------
#
# NMRA-DCC Instructions
#
#                        11AAAAAA 0 AAAAAAAA               lange Adresse
#              berechnen:  [Adresse // 256 | 0xc0)] [Adresse & 0xff]
#   {instruction-bytes} = CCCDDDDD
#                         CCCDDDDD 0 DDDDDDDD
#                         CCCDDDDD 0 DDDDDDDD 0 DDDDDDDD
#
#   CCCDDDDD = CCCGGGGG or CCCGTTTT
#
#   CCC = 000: Decoder and Consist Control Instruction
#              Decoder Control (GTTTT=0TTTF):
#                   {instruction byte} = 0000TTTF
#                   {instruction byte} = 0000TTTF 0 DDDDDDDD
#
#              TTT = 101 Set Advanced Addressing (CV#29 bit 5==F)
#                    {instruction byte} = 00001011: Lange Adresse benutzen
#                                         00001010: kurze Adresse benutzen 
#
#                    111 Ack anfordern
#                    {instruction byte} = 00001111
#
#  @TODO
#              Consist Control (GTTTT=1TTTT)
#                    {instruction bytes} = 0001TTTT 0 0AAAAAAA
#  /@TODO
#
#         001: Advanced Operation Instructions 001GGGGG
#                    {instruction bytes} = 001GGGGG 0 DDDDDDDD
#                    GGGGG = 11111: 128 Speed Step Control
#                            DDDDDDDD = DSSSSSSS
#
#                            D=1: vorwärts,
#                            D=0: rückwärts
#                            SSSSSSS = 0:        Stop
#                                      1:        Notstop
#                                      2 .. 127: Fahrstufe 1..126
#
#         010: Speed and Direction Instruction for reverse operation
#         011: Speed and Direction Instruction for forward operation
#
#         01DCSSSS wie RP 9.2 - Fahren (sh. "CSSSS berechnen" unter "Baseline instructions")
#           D = 0: rueckwaerts, D=1: vorwaerts
#            CSSSS: Fahrstufen = 0: Stop, 1-28: ((Fahrstufe + 3 & 0x01) << 4) | ((Fahrstufe + 3) << 1)
#
#         100: Function Group One Instruction
#
#         100DDDDD Funktionen Gruppe 0 u. FL
#            DDDDD: 10000 FL, 01000 F4 ... 00001 F1
#
# ----------------------------------------------------------------------
#
#  Version 0.5ß 2024-06-05
#
import machine
import rp2
from micropython import const
import utime

DEBUG = False

class ELECTRICAL:
    
    # hier Verbindungen einstellen
    POWER_PIN = const(22)
    BRAKE_PIN = const(20)
    PWM_PIN = const(19)
    DIR_PIN = const(21)
    ACK_PIN = const(27)


    LMD18200_QUIESCENT_CURRENT = const(17.0)
    LMD18200_SENS_SHUNT = const(20000)                # Ohm
    AREF_VOLT = const(3300)                           # mV !!
    DENOISE_SAMPLES = const(200)                      # Anzahl der Messzyklen, für Rauschunterdrückung
    LMD18200_SENS_AMPERE_PER_AMPERE = const(0.000377) # Empfindlichkeit: 377µA / A lt. Datenblatt
    SHORT = const(1000)                               # erlaubter max. Strom in mA
    PREAMBLE = const(14)                              # Präambel f. Servicemode
    ACK_TRESHOLD = const(40)                          # Hub f. Ack
    CURRENT_SMOOTHING = const(0.175)                  # Glättung der Messergebnisse versuchen
    
    # preamble 0 11111111 0 00000000 0 11111111 1
    IDLE =      [ const(0b11111111111111111111111111111111), const(0b11110111111110000000000111111111) ]
    # preamble 0 00000000 0 00000000 0 00000000 1
    EMERG =     [ const(0b11111111111111111111111111111111), const(0b11110000000000010000010010000011) ]
    # long-preamble 0 01111111 0 00001000 0 01110111 1
    
    locos = []
    
    # DCC- und H-Bridge-LMD18200T-Modul elektrische Steuerung
    def __init__(self):   
        self.brake = machine.Pin(self.BRAKE_PIN, machine.Pin.OUT)
        self.pwm = machine.Pin(self.PWM_PIN, machine.Pin.OUT)
        self.power = machine.Pin(self.POWER_PIN, machine.Pin.OUT)
        self.dir_pin = machine.Pin(self.DIR_PIN, machine.Pin.OUT)
        self.ack = machine.ADC(machine.Pin(self.ACK_PIN))
        self.power_state = self.power.value()
        self.buffer_dirty = False
        self.emergency = False
        self.ringbuffer = []
        
        # freq = 500_000 # 2.0us clock cycle
        self.statemachine = rp2.StateMachine(0, self.dccbit, freq=500000, set_base=machine.Pin(self.dir_pin))
        self.statemachine.active(1)
        self.messtimer = utime.ticks_ms()
        
         
    # LMD18200T
    # Logiktabelle:
    # PWM | Dir | Brake | Output
    # ----+-----+-------+-------
    #  H  |  H  |   L   | A1, B2 -> A = VCC, B = GND
    #  H  |  L  |   L   | A2, B1 -> A = GND, B = VCC
    #  L  |  X  |   L   | A1, B1 -> Brake (Motor kurzgeschlossen über VCC
    #  H  |  H  |   H   | A1, B1 -> Brake (Motor kurzgeschlossen über VCC
    #  H  |  L  |   H   | A2, B2 -> Brake (Motor kurzgeschlossen über GND
    #  L  |  X  |   H   | None   -> Power off
    def power_off(self):
        self.brake.value(1)  
        self.pwm.value(0)
        self.power.value(False)
        self.power_state = False
        self.emergency = False

    def power_on(self):
        self.pwm.value(1)
        self.power_time = utime.ticks_ms()
        self.brake.value(0)  
        self.power.value(True)
        self.power_state = True
        self.chk_short()

    def raw2mA(self, analog_value):
        analog_value = analog_value * self.AREF_VOLT / 65535  # ADC mappt auf 0..65535
        analog_value /= self.LMD18200_SENS_SHUNT  # Rsense
        return (analog_value / self.LMD18200_SENS_AMPERE_PER_AMPERE) - self.LMD18200_QUIESCENT_CURRENT  # lt. Datenblatt 377 µA / A +/- 10 %

    def get_current(self):
        analog_value = 0
        max_value = 0
        for i in range(0, self.DENOISE_SAMPLES):
            analog_value = (self.ack.read_u16() - analog_value) * self.CURRENT_SMOOTHING + analog_value * (1 - self.CURRENT_SMOOTHING)
            max_value = max(analog_value, max_value)
        return round(self.raw2mA(max_value))
    
    def chk_short(self):
        if self.get_current() > self.SHORT: # Kurzschluss (ggf. im Servicemode
            raise(RuntimeError("!!! KURZSCHLUSS !!!"))

    def emergency_stop(self):
        self.emergency = True
    
    # Geschwindigkeitscode 14 Fahrstufen
    def speed_control_14steps(self, direction, speed):
        pass
    
    # Geschwindigkeitscode 128 Fahrstufen
    def speed_control_128steps(self, direction, speed):
        if speed == -1:
            speed = 1
        elif speed == 0:
            speed = 0
        else:
            if speed < 126:
                speed += 2
            speed &= 0x7e
        speed |= (direction << 7)
        speed &= 0xff
        return speed

    # Geschwindigkeitscode 28 Fahrstufen
    def speed_control_28steps(self, direction, speed):
        cssss = 0
        speed = min(speed, 28)
        if speed == -1:                  # Notstop
           cssss = 0b00000
        elif 0 <= speed <= 28:
           if speed == 0:
               cssss = 0b00000
           else:
               temp = speed + 3
               c = (temp & 0b1) << 4
               ssss = temp >> 1
               cssss = c | ssss

        return (0b01000000 | direction << 5 | cssss) & 0xff
        
   
    def to_bin(self, num):
        stream = 0
        for j in range(0, 8):
            if num & 1 << (7 - j):
                stream |= 1
            stream <<= 1
        return stream

    def prepare(self, packet=[]):  # Daten in den Puffer stellen
        stream = 0
        bits = 0
        padding = 0
        if 2 <= len(packet) <= 5:  # Anzahl Bytes ohne XOR
            # Streamlänge = jedem Byte ein 0 voran, das XOR-byte + 1 ans Ende + Preamble + Padding auf Wortgrenze (32 Bit)
            err = 0;
            for byte in packet:
                err ^= byte
            packet.append(err)
            preamble = self.PREAMBLE
            bits = preamble + len(packet) * 9 + 1
            padding = 32 - (bits % 32) # links mit 1 erweitern bis Wortgrenze
            for i in range(0, padding + preamble):
                stream |= 1
                stream <<= 1
            for i in packet:
                stream <<= 9
                stream |= self.to_bin(i)
            stream |= 1
        return (padding + bits) // 32, stream  # Anzahl der Worte + Bitstream
        
    def generate_instructions(self):
        words  = []
        lengths = []
        for loco in self.locos:
            # Richtung, Geschwindigkeit
            instruction = []
            if loco.use_long_address:
                instruction.append(192 | (loco.address // 256))
                instruction.append(loco.address & 0xff)
            else:
                instruction.append(loco.address)
            richtung = loco.current_speed["Dir"]
            fahrstufe = loco.current_speed["FS"]
            if loco.speedsteps == 128:
                speed = self.speed_control_128steps(richtung, fahrstufe)
                instruction.append(0b00111111)
                instruction.append(speed)
            elif loco.speedsteps == 28:
                speed = self.speed_control_28steps(richtung, fahrstufe)
                instruction.append(speed)
            elif loco.speedsteps == 14: # @TODO
                speed = self.speed_control_14steps(richtung, fahrstufe)
                pass
            else:
                pass

            l, w = self.prepare(instruction)
            words.append(w)
            lengths.append(l)
        
            # Funktionen
            for f in loco.functions:
                instruction = []
                if loco.use_long_address:
                    instruction.append(192 | (loco.address // 256))
                    instruction.append(loco.address & 0xff)
                else:
                    instruction.append(loco.address)
                    
                instruction.append(f)

                l, w = self.prepare(instruction)
                words.append(w)
                lengths.append(l)
                
        return lengths, words
            
    def buffering(self):
        buffer = []
        if self.emergency == True:
            for i in range(5):
                for e in self.EMERG:
                    buffer.append(e)
            self.emergency = False
            
        else:
            l, w = self.generate_instructions()
            for i in range(len(l)):
                while l[i] > 0:
                    l[i] -= 1
                    buffer.append(w[i] >> l[i] * 32 & 0xffffffff)

            if buffer == []:
                buffer = self.IDLE

        return buffer

    def send2track(self):
        buffer = self.IDLE
        try:
            if self.power_state == True:
                if (utime.ticks_ms() - self.messtimer > 100):
                    self.chk_short()
                    self.messtimer = utime.ticks_ms()
     
                if self.buffer_dirty:
                    buffer = self.buffering()
                    self.ringbuffer = buffer
                else:
                    buffer = self.ringbuffer
                if not DEBUG:
                    state = machine.disable_irq()
                for word in buffer:
                     self.statemachine.put(word)
    
                if not DEBUG:
                    machine.enable_irq(state)
                self.buffer_dirty = False

        except KeyboardInterrupt:
            raise(KeyboardInterrupt("SIGINT"))

    # 0 = 100µs = 50 Takte, 1 = 58µs = 29 Takte 
    @rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT, autopull=True)
    def dccbit():
        label("bitstart")
        set(pins, 1)[26]
        out(x, 1)
        jmp(not_x, "is_zero")
        set(pins, 0)[27]
        jmp("bitstart")
        label("is_zero")
        nop()[20]
        set(pins, 0)[28]
        nop()[20]

# --------------------------------------

class OPERATIONS(ELECTRICAL):
    
    def __init__(self, address=None, use_long_address=False, speedsteps = 128, electrical=None):
        super().__init__()
        if address != None:
            self.address = address
            self.use_long_address = use_long_address
            self.current_speed = {"Dir": 1, "FS": 0}
            self.speedsteps = speedsteps
            self.functions = [0b10000000, 0b10110000, 0b10100000]
            super().locos.append(self)

    def deinit(self):
        if self.power_state == True:
            self.power_off()
        utime.sleep_ms(100)
        self.emergency_stop()
        self = None
    
    def begin(self):
        self.power_on()
        self.chk_short()
        utime.sleep_ms(100)
    
    def loop(self):
        if self.power_state == False:
            raise(RuntimeError("Power is off"))
        self.send2track()    # scheduler: Funktion liefert die DCC-Instruktionen an das Gleis

    # Funktionsgruppen-ID
    def get_function_group_index(self, function_nr):
        if function_nr < 5:
            function_group = 0
        elif function_nr < 9:
            function_group = 1
        else:
            function_group = 2
        return function_group
    
    # Shift für die Funktionsbytes    
    def get_function_shift(self, function_nr):
        if function_nr == 0:
            function_shift = 4
        else:
            function_shift = ((function_nr - 1) % 4)
        return function_shift

    # Funktionscode
    def function_control(self, funktion=0):
        f =  0b10000000
        if 0 <= funktion <= 4:
            pass
        elif 5 <= funktion <= 8:
            f |= 0b110000
        elif 9 <= funktion <= 12:
            f |= 0b100000
        return f

    def function_on(self, function_nr):
        self.set_function(function_nr)
        
    def function_off(self, function_nr):
        self.set_function(function_nr, False)

    # Funktion aktiv oder inaktiv?
    def get_function(self, function_nr):
        status = False
        if 0 <= function_nr <= 12:
            function_group = self.get_function_group_index(function_nr)
            status = (self.functions[function_group] & (1 << self.get_function_shift(function_nr))) != 0 
        return status
        
    # Funktionsbits setzen und an Lok senden
    def set_function(self, function_nr, status = True):
        if 0 <= function_nr <= 12:
            function_group = self.get_function_group_index(function_nr)
            instruction_prefix = self.function_control(function_nr)
            if status == True:
                self.functions[function_group] |= ((1 << self.get_function_shift(function_nr)) | instruction_prefix)
            else:
                self.functions[function_group] &= (~(1 << self.get_function_shift(function_nr)) | instruction_prefix)
        self.buffer_dirty = True
        
    # fahre mit 14 oder 28/128 FS (128 bevorzugt)
    def drive(self, richtung, fahrstufe):  # Fahrstufen
        self.current_speed = {"Dir": richtung, "FS": fahrstufe}
        self.buffer_dirty = True
        
    
# --------------------------------------