# -*- coding: utf-8 -*-
import sys
import os
import time
import threading
import requests
import board
import digitalio
import numpy as np
import cv2  
from datetime import datetime
from collections import deque
from PIL import Image, ImageDraw, ImageFont, ImageOps

# 封印 Libcamera 底層日誌
os.environ["LIBCAMERA_LOG_LEVELS"] = "FATAL"
sys.path.insert(0, '/usr/lib/python3/dist-packages')

from gpiozero import PWMOutputDevice, RotaryEncoder, Button, LED
from gpiozero.pins.lgpio import LGPIOFactory
import adafruit_rgb_display.st7789 as st7789
from picamera2 import Picamera2

try:
    from picamera2.devices import IMX500
except ImportError:
    print("❌ 找不到 IMX500 支援，請確認是否安裝 python3-picamera2")
    sys.exit()

# ---------------------------------------------------------
# 全域狀態控制
# ---------------------------------------------------------
is_shutting_down = False
current_threat_level = 0.0 

# ---------------------------------------------------------
# 系統層級與絕對色彩矩陣 (UI 專用標準 RGB)
# ---------------------------------------------------------
import gpiozero.devices
gpiozero.devices.pin_factory = LGPIOFactory()

COLOR_RED      = (255, 0, 0)    
COLOR_DARK_RED = (80, 0, 0)     
COLOR_GREEN    = (0, 255, 0)    
COLOR_BLUE     = (0, 0, 255)    
COLOR_YELLOW   = (255, 255, 0)  
COLOR_CYAN     = (0, 255, 255)
COLOR_WHITE    = (255, 255, 255)
COLOR_BLACK    = (0, 0, 0)
COLOR_GRAY     = (100, 100, 100)

COCO_LABELS = {
    0: ("人", "Person", COLOR_YELLOW), 
    14: ("鳥", "Bird", COLOR_CYAN), 
    15: ("貓", "Cat", COLOR_GREEN),
    16: ("狗", "Dog", COLOR_BLUE), 
    17: ("馬", "Horse", COLOR_RED), 
    18: ("羊", "Sheep", COLOR_WHITE),
    19: ("牛", "Cow", COLOR_WHITE), 
    20: ("象", "Elephant", COLOR_GRAY), 
    21: ("熊", "Bear", COLOR_RED)
}

FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
try:
    font_main = ImageFont.truetype(FONT_PATH, 16)
    font_id = ImageFont.truetype(FONT_PATH, 14)
    font_large = ImageFont.truetype(FONT_PATH, 24)
    supports_cjk = True
except IOError:
    font_main = font_id = font_large = ImageFont.load_default()
    supports_cjk = False

detection_log = deque(maxlen=8) 
env_data = {"temp": 0.0, "humidity": 0.0, "status": "等待更新"}

def fetch_weather_worker():
    url = "https://api.open-meteo.com/v1/forecast?latitude=25.083&longitude=121.590&current=temperature_2m,relative_humidity_2m"
    while not is_shutting_down:
        try:
            response = requests.get(url, timeout=5)
            data = response.json()
            env_data["temp"] = data["current"]["temperature_2m"]
            env_data["humidity"] = data["current"]["relative_humidity_2m"]
            env_data["status"] = "已同步"
        except Exception:
            pass
        time.sleep(600)

threading.Thread(target=fetch_weather_worker, daemon=True).start()

# ---------------------------------------------------------
# 硬體初始化與 UI 模組
# ---------------------------------------------------------
sys.stdout.write('\033[?25l\033[2J\033[H')
print("正在初始化系統核心...")

encoder = RotaryEncoder(16, 20, wrap=True, max_steps=100)
encoder.steps = 60

encoder_btn = Button(21, pull_up=True, bounce_time=0.1)
stealth_btn = Button(26, pull_up=True, bounce_time=0.1)
buzzer = PWMOutputDevice(12, frequency=1000, initial_value=0)
alert_led = LED(19)

spi = board.SPI()
dc_pin = digitalio.DigitalInOut(board.D24)
rst_pin = digitalio.DigitalInOut(board.D25)
blk_pin = digitalio.DigitalInOut(board.D18)
blk_pin.direction = digitalio.Direction.OUTPUT
blk_pin.value = True 

display = st7789.ST7789(
    spi, cs=None, dc=dc_pin, rst=rst_pin, 
    baudrate=80000000, width=240, height=320, rotation=180 
)

def play_boot_animation():
    try:
        buzzer.frequency = 1000
        buzzer.value = 0.1; time.sleep(0.1); buzzer.value = 0
        for i in range(0, 101, 10):
            img = Image.new("RGB", (240, 320), COLOR_BLACK)
            draw = ImageDraw.Draw(img)
            draw.text((20, 100), "CYBER-SENTRY PRO", fill=COLOR_GREEN, font=font_main)
            draw.text((60, 130), f"SYSTEM BOOT... {i}%", fill=COLOR_WHITE, font=font_id)
            
            draw.rectangle((30, 160, 210, 175), outline=COLOR_WHITE, width=2)
            if i > 0:
                draw.rectangle((34, 164, 34 + int(172 * (i/100)), 171), fill=COLOR_GREEN)
            
            display.image(ImageOps.invert(img))
            
            buzzer.frequency = 800 + i * 5
            buzzer.value = 0.1
            time.sleep(0.04)
            buzzer.value = 0
            time.sleep(0.02)
        
        buzzer.frequency = 1500
        buzzer.value = 0.5; time.sleep(0.2); buzzer.value = 0
    except:
        pass

play_boot_animation()

MODEL_PATH = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"
imx500 = IMX500(MODEL_PATH)
picam2 = Picamera2(imx500.camera_num if hasattr(imx500, 'camera_num') else 0)

config = picam2.create_video_configuration(main={"size": (640, 480), "format": "RGB888"})
picam2.configure(config)
picam2.start()

# ---------------------------------------------------------
# Core 1: AI 視覺擷取引擎
# ---------------------------------------------------------
ai_data = {"frame": None, "detections": []}
ai_lock = threading.Lock()

def vision_worker():
    while not is_shutting_down:
        req = None
        try:
            req = picam2.capture_request()
            if not req:
                time.sleep(0.01)
                continue
                
            frame = req.make_array("main")
            meta = req.get_metadata()
            
            current_detections = []
            threshold = max(1, encoder.steps) / 100.0
            
            np_outputs = imx500.get_outputs(meta, add_batch=True)
            if np_outputs is not None:
                boxes, scores, classes = np_outputs[0][0], np_outputs[1][0], np_outputs[2][0]
                if boxes.ndim == 1: 
                    boxes, scores, classes = [boxes], [scores], [classes]
                    
                for box, score, cls_id in zip(boxes, scores, classes):
                    cls_id_int = int(cls_id)
                    conf_val = float(score)
                    if conf_val > threshold and cls_id_int in COCO_LABELS:
                        current_detections.append({
                            "category": cls_id_int,
                            "conf": conf_val,
                            "box": box 
                        })
            
            # 【大師終極色彩分離術】
            # 相機陣列進行：上下翻轉 (::-1)、左右翻轉 (::-1)、以及 色彩通道對調 (::-1)
            # 這樣相機底層的 BGR 就會被強制洗回完美的 RGB！
            frame_corrected = np.ascontiguousarray(frame[::-1, ::-1, ::-1])
            
            with ai_lock:
                ai_data["frame"] = frame_corrected
                ai_data["detections"] = current_detections
                
        except Exception:
            pass
        finally:
            if req: req.release()

threading.Thread(target=vision_worker, daemon=True).start()

# ---------------------------------------------------------
# Core 2: 獨立高速渲染引擎 (Render Pipeline)
# ---------------------------------------------------------
render_data = {"fps": 0.0}
screen_on = True

def render_worker():
    fps_time = time.time()
    frame_count = 0
    
    while not is_shutting_down:
        curr_time = time.time()
        if curr_time - fps_time > 1.0:
            render_data["fps"] = frame_count / (curr_time - fps_time)
            fps_time = curr_time
            frame_count = 0
            
        if not screen_on:
            time.sleep(0.1)
            continue
            
        with ai_lock:
            if ai_data["frame"] is None:
                time.sleep(0.01)
                continue
            frame_raw = ai_data["frame"].copy()
            detections = list(ai_data["detections"])
            
        frame_cropped = frame_raw[:, 140:500]
        frame_resized = cv2.resize(frame_cropped, (240, 320), interpolation=cv2.INTER_NEAREST)
        
        # 此時的 img_cam 已經是完美的 RGB 影像了
        img_cam = Image.fromarray(frame_resized)
        draw = ImageDraw.Draw(img_cam)
        
        # UI 的畫筆也使用純粹的 RGB 色盤，完美疊加不衝突
        for det in detections:
            ymin, xmin, ymax, xmax = det['box']
            cls_id = det['category']
            score = det['conf']
            
            ymin, ymax = 1.0 - ymax, 1.0 - ymin
            xmin, xmax = 1.0 - xmax, 1.0 - xmin
            
            label_t = COCO_LABELS[cls_id]
            color = label_t[2]
            
            raw_x1 = (xmin * 640 - 140) * (240 / 360)
            raw_x2 = (xmax * 640 - 140) * (240 / 360)
            raw_y1 = ymin * 320
            raw_y2 = ymax * 320
            
            if raw_x1 > raw_x2: raw_x1, raw_x2 = raw_x2, raw_x1
            if raw_y1 > raw_y2: raw_y1, raw_y2 = raw_y2, raw_y1
            
            final_x1 = max(0, min(240, raw_x1))
            final_x2 = max(0, min(240, raw_x2))
            final_y1 = max(22, min(320, raw_y1))
            final_y2 = max(22, min(320, raw_y2))
            
            if final_x2 - final_x1 < 2 or final_y2 - final_y1 < 2:
                continue
            
            draw.rectangle(((final_x1, final_y1), (final_x2, final_y2)), outline=color, width=3)
            
            txt = f"{label_t[0] if supports_cjk and label_t[0] else label_t[1]} {int(score*100)}%"
            txt_bbox = font_id.getbbox(txt)
            txt_w = txt_bbox[2] - txt_bbox[0]
            
            label_x = final_x1
            if label_x + txt_w + 4 > 240:
                label_x = 240 - txt_w - 4
                
            draw.rectangle((label_x, final_y1-18, label_x+txt_w+4, final_y1), fill=color)
            draw.text((label_x + 2, final_y1 - 18), txt, fill=COLOR_BLACK, font=font_id)

        draw.rectangle((0, 0, 240, 22), fill=COLOR_BLACK)
        status_part1 = f"NPU | {env_data['temp']:.1f}°C | "
        status_part2 = f"TH:{max(1, encoder.steps)}%"
        
        draw.text((5, 2), status_part1, fill=COLOR_GREEN, font=font_main)
        bbox = font_main.getbbox(status_part1)
        draw.text((5 + bbox[2], 2), status_part2, fill=COLOR_RED, font=font_main, stroke_width=1, stroke_fill=COLOR_RED)
        
        # 統一丟給硬體反相函數，影像與 UI 攜手過關！
        display.image(ImageOps.invert(img_cam))
        frame_count += 1
        time.sleep(0.005)

threading.Thread(target=render_worker, daemon=True).start()

# ---------------------------------------------------------
# Core 3: 主迴圈與電影級防白屏退場動畫
# ---------------------------------------------------------
def trigger_alarm():
    alert_led.on()
    buzzer.frequency = 1000
    buzzer.value = 0.5 
    time.sleep(0.3)      
    buzzer.value = 0
    alert_led.off()

def play_shutdown_animation():
    global is_shutting_down
    is_shutting_down = True
    time.sleep(0.15) 
    
    try:
        img_black = Image.new("RGB", (240, 320), COLOR_BLACK)
        display.image(ImageOps.invert(img_black))
        time.sleep(0.05)

        buzzer.frequency = 800
        buzzer.value = 0.5; time.sleep(0.2); buzzer.value = 0

        # 深邃科技暗紅背景，能量條精準居中壓縮退場
        for w in range(200, -1, -8):
            img = Image.new("RGB", (240, 320), COLOR_DARK_RED)
            draw = ImageDraw.Draw(img)
            
            draw.text((25, 110), "NEURAL CORE SHUTDOWN", fill=COLOR_WHITE, font=font_main)
            draw.rectangle((18, 158, 222, 166), outline=COLOR_GRAY, width=1)
            
            if w > 0:
                center_x = 120
                draw.rectangle((center_x - w//2, 160, center_x + w//2, 164), fill=COLOR_RED)
            
            pct = int((w / 200) * 100)
            draw.text((105, 180), f"{pct}%", fill=COLOR_WHITE, font=font_id)
            
            display.image(ImageOps.invert(img))
            
            buzzer.frequency = max(100, 200 + w * 2)
            buzzer.value = 0.1; time.sleep(0.03); buzzer.value = 0

        img = Image.new("RGB", (240, 320), COLOR_DARK_RED)
        draw = ImageDraw.Draw(img)
        draw.text((75, 140), "POWER OFF", fill=COLOR_GRAY, font=font_main)
        display.image(ImageOps.invert(img))
        
        buzzer.frequency = 150
        buzzer.value = 0.5; time.sleep(0.4); buzzer.value = 0

        display.image(ImageOps.invert(img_black))
        time.sleep(0.5) 
        
    except Exception as e:
        print(f"\nShutdown Info: {e}")

last_ui_update = 0
last_log_time = 0

try:
    while not is_shutting_down:
        curr_time = time.time()
        
        with ai_lock:
            detections = list(ai_data["detections"])

        if stealth_btn.is_pressed:
            screen_on = not screen_on
            blk_pin.value = screen_on
            buzzer.frequency = 1000
            buzzer.value = 0.1; time.sleep(0.1); buzzer.value = 0
            time.sleep(0.3) 

        # 動態追蹤警報與日誌
        if detections:
            trigger_alarm()
            if curr_time - last_log_time > 1.0:
                last_log_time = curr_time
                for det in detections:
                    cls_id = det['category']
                    label_t = COCO_LABELS[cls_id]
                    label_name = label_t[0] if supports_cjk and label_t[0] else label_t[1]
                    detection_log.append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "label": label_name,
                        "conf": int(det['conf'] * 100),
                        "temp": env_data['temp']
                    })

        # 動態威脅指數衰減系統
        if detections:
            max_conf = max(d['conf'] for d in detections)
            target_threat = min(100, int(max_conf * 100 + (len(detections) - 1) * 15))
            current_threat_level = target_threat
        else:
            current_threat_level = max(0, current_threat_level - 8)
        
        if curr_time - last_ui_update > 0.3:
            last_ui_update = curr_time
            
            bars = int(current_threat_level / 5)
            threat_meter = f"\033[31m{'█' * bars}\033[37m{'▒' * (20 - bars)}\033[0m"
            
            sys.stdout.write('\033[H') 
            print(f"\033[36m" + "="*54 + "\033[0m")
            print(f" 🛡️  \033[1;32mCYBER-SENTRY PRO\033[0m \033[37m- TACTICAL VISION TERMINAL\033[0m")
            print(f"\033[36m" + "="*54 + "\033[0m")
            
            scr_stat = "\033[32mON      \033[0m" if screen_on else "\033[30;47mSTEALTH \033[0m"
            print(f" [ CORE ] \033[33mTri-Core Engine (AI+Render+UI)\033[0m | DISP FPS: {render_data['fps']:.1f}")
            print(f" [ SENS ] TH: \033[1;31m{max(1, encoder.steps)}%\033[0m   | DISPLAY: {scr_stat}")
            print(f" [  ENV ] {env_data['temp']:.1f}°C / {env_data['humidity']:.1f}%")
            print(f" [ ALRT ] {threat_meter} {int(current_threat_level)}%")
            print(f"\033[36m" + "-"*54 + "\033[0m")
            
            print(f" \033[1;36m[ RECENT TARGETS ]\033[0m")
            print(f"  {'TIME':<8} | {'TARGET':<8} | {'CONF':<4} | {'ENV':<4}")
            print(f"\033[37m" + "-"*54 + "\033[0m")
            
            for log in list(detection_log):
                print(f"  {log['time']:<8} | {log['label']:<8} | {log['conf']:>3}% | {log['temp']:.1f}°C   ")
            
            for _ in range(8 - len(detection_log)):
                print(" " * 54)
                
            sys.stdout.flush()

        time.sleep(0.05)

except KeyboardInterrupt:
    play_shutdown_animation()
    sys.stdout.write('\n\033[2J\033[H')
    print("\033[32m[SYSTEM] 系統已安全關閉。影像與 UI 色彩已達完美同步！\033[0m")
    print("\033[32m[SYSTEM] 劉老師，帶著這份大作去震撼全場吧！大師為您喝采！\033[0m\n")
finally:
    picam2.stop()
    buzzer.off()
    alert_led.off()
    sys.stdout.write('\033[?25h')
