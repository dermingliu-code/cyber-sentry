# -*- coding: utf-8 -*-
import sys
import os
import time
import threading
import requests
import board
import digitalio
import numpy as np
from datetime import datetime
from collections import deque
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
# 系統層級與硬體初始化
# ---------------------------------------------------------
import gpiozero.devices
gpiozero.devices.pin_factory = LGPIOFactory()

COCO_LABELS = {
    0: ("人", "Person"), 14: ("鳥", "Bird"), 15: ("貓", "Cat"),
    16: ("狗", "Dog"), 17: ("馬", "Horse"), 18: ("羊", "Sheep"),
    19: ("牛", "Cow"), 20: ("象", "Elephant"), 21: ("熊", "Bear")
}

FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
try:
    font_main = ImageFont.truetype(FONT_PATH, 16)
    font_id = ImageFont.truetype(FONT_PATH, 14)
    supports_cjk = True
except IOError:
    font_main = ImageFont.load_default()
    font_id = ImageFont.load_default()
    supports_cjk = False

detection_log = deque(maxlen=10) 
env_data = {"temp": 0.0, "humidity": 0.0, "status": "等待更新"}

def fetch_weather_worker():
    url = "https://api.open-meteo.com/v1/forecast?latitude=25.083&longitude=121.590&current=temperature_2m,relative_humidity_2m"
    while True:
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            env_data["temp"] = data["current"]["temperature_2m"]
            env_data["humidity"] = data["current"]["relative_humidity_2m"]
            env_data["status"] = "已同步"
        except Exception:
            env_data["status"] = "網路錯誤"
        time.sleep(600)

threading.Thread(target=fetch_weather_worker, daemon=True).start()

sys.stdout.write('\033[2J\033[H')
print("正在初始化硬體資源...")

encoder = RotaryEncoder(16, 20, wrap=True, max_steps=100)
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

# 【修正】設定為直立模式 width=240, height=320, rotation=0 (或依您的面板排線可改為180)
display = st7789.ST7789(
    spi, cs=None, dc=dc_pin, rst=rst_pin, 
    baudrate=64000000, width=240, height=320, rotation=0 
)

MODEL_PATH = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"
imx500 = IMX500(MODEL_PATH)
picam2 = Picamera2(imx500.camera_num if hasattr(imx500, 'camera_num') else 0)

# 【修正】相機必須使用 640x480，確保 NPU 能正常載入與推論！
config = picam2.create_video_configuration(main={"size": (640, 480), "format": "RGB888"})
picam2.configure(config)
picam2.start()

def trigger_alarm():
    alert_led.on()
    buzzer.value = 0.5 
    time.sleep(1)      
    buzzer.value = 0
    alert_led.off()

# ---------------------------------------------------------
# 主迴圈
# ---------------------------------------------------------
screen_on = True
fps_time = time.time()
frame_count = 0
fps = 0.0
last_ui_update = 0

try:
    sys.stdout.write('\033[2J\033[H')
    
    while True:
        curr_time = time.time()
        frame_count += 1
        
        if curr_time - fps_time > 1.0:
            fps = frame_count / (curr_time - fps_time)
            fps_time = curr_time
            frame_count = 0

        if stealth_btn.is_pressed:
            screen_on = not screen_on
            blk_pin.value = screen_on
            buzzer.value = 0.1; time.sleep(0.1); buzzer.value = 0
            time.sleep(0.3) 
            
        # 1. 確保影像與 NPU Metadata 同步擷取
        req = picam2.capture_request()
        frame_raw = req.make_array("main")
        metadata = req.get_metadata()
        req.release()
        
        detections = []
        confidence_threshold = max(1, encoder.steps) / 100.0
        
        # 2. 解析 NPU 偵測結果
        try:
            np_outputs = imx500.get_outputs(metadata, add_batch=True)
            if np_outputs is not None:
                boxes, scores, classes = np_outputs[0][0], np_outputs[1][0], np_outputs[2][0]
                if boxes.ndim == 1: 
                    boxes, scores, classes = [boxes], [scores], [classes]
                    
                for box, score, cls_id in zip(boxes, scores, classes):
                    if score > confidence_threshold:
                        cls_id_int = int(cls_id)
                        conf_val = float(score)
                        
                        detections.append({
                            "category": cls_id_int,
                            "conf": conf_val,
                            "box": box  # [ymin, xmin, ymax, xmax] 在 0.0~1.0 之間
                        })
                        
                        label_tuple = COCO_LABELS.get(cls_id_int, (None, None))
                        label_name = label_tuple[0] if supports_cjk and label_tuple[0] else label_tuple[1]
                        if not label_name: label_name = f"ID:{cls_id_int}"
                        
                        detection_log.append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "label": label_name,
                            "conf": int(conf_val * 100),
                            "temp": env_data['temp']
                        })
        except Exception as e:
            pass 

        if detections:
            trigger_alarm()

        # 3. 處理影像渲染 (相機 640x480 -> 螢幕 240x320)
        if screen_on:
            # 【修正】將 RGB 轉換為 BGR 解決藍紅反轉問題
            frame_bgr = frame_raw[:, :, ::-1]
            
            img_cam = Image.fromarray(frame_bgr)
            
            # 將 640x480 (4:3) 中心裁切為 360x480 (3:4)，完美符合直立螢幕比例
            img_cam = img_cam.crop((140, 0, 500, 480))
            
            # 縮小到硬體螢幕尺寸 240x320
            img_cam = img_cam.resize((240, 320), Image.Resampling.NEAREST)
            
            draw = ImageDraw.Draw(img_cam)
            
            # 繪製動態捕捉框
            for det in detections:
                ymin, xmin, ymax, xmax = det['box']
                cls_id = det['category']
                score = det['conf']
                
                color = (0, 0, 255) # BGR 格式下，紅色是 (0, 0, 255)
                if cls_id == 0: color = (0, 255, 255) # 黃色
                
                # 計算裁切與縮放後的正確座標
                abs_x1, abs_x2 = xmin * 640, xmax * 640
                abs_y1, abs_y2 = ymin * 480, ymax * 480
                
                # 扣除 X 軸中心裁切的 140 偏移量，並乘上 240/360 的縮放比
                final_x1 = (abs_x1 - 140) * (240 / 360)
                final_x2 = (abs_x2 - 140) * (240 / 360)
                # Y 軸乘上 320/480 的縮放比
                final_y1 = abs_y1 * (320 / 480)
                final_y2 = abs_y2 * (320 / 480)
                
                draw.rectangle(((final_x1, final_y1), (final_x2, final_y2)), outline=color, width=2)
                
                label_t = COCO_LABELS.get(cls_id, (None, None))
                txt = f"{label_t[0] if supports_cjk and label_t[0] else label_t[1]} {int(score*100)}%"
                draw.text((final_x1 + 3, final_y1 + 1), txt, fill=(255, 255, 255), font=font_id)

            # 繪製頂部黑底狀態列 (寬度 240)
            draw.rectangle((0, 0, 240, 22), fill=(0, 0, 0))
            draw.text((5, 2), f"NPU | {env_data['temp']:.1f}°C | TH:{max(1, encoder.steps)}%", fill=(0, 255, 0), font=font_main)
            
            # 【修正】執行硬體反相處理 (如果您的螢幕仍然是白色底色，請保留這行)
            display_img = ImageOps.invert(img_cam)
            
            display.image(display_img)
        
        # 4. 更新終端機
        if curr_time - last_ui_update > 0.25:
            last_ui_update = curr_time
            sys.stdout.write('\033[H') 
            print(f"==================================================")
            print(f" 🛡️ [CYBER-SENTRY PRO] 邊緣 AI 戰術儀表板")
            print(f"==================================================")
            
            screen_status = "\033[32mON(視窗) \033[0m" if screen_on else "\033[30;47mSTEALTH \033[0m"
            alert_status = "\033[41m⚠️ 警報觸發\033[0m" if detections else "\033[32m安全監控中\033[0m"
            
            print(f" 系統狀態 : {alert_status:15} | 螢幕: {screen_status:12}")
            print(f" 效能數據 : FPS {fps:.1f}           | 閾值: {max(1, encoder.steps)}%")
            print(f" 環境數據 : 氣溫 {env_data['temp']:.1f}°C")
            print(f"--------------------------------------------------")
            print(f" [\033[36m最新 10 筆偵測紀錄\033[0m]") 
            print(f"  {'時間':<8} | {'目標':<8} | {'信心度':<6} | {'環境':<4}")
            print(f"--------------------------------------------------")
            
            for log in list(detection_log):
                print(f"  {log['time']:<8} | {log['label']:<8} | {log['conf']}%     | {log['temp']:.1f}°C")
            
            for _ in range(10 - len(detection_log)):
                print("                                                  ")
            sys.stdout.flush()

except KeyboardInterrupt:
    sys.stdout.write('\n\033[2J\033[H')
    print("收到中斷訊號，正在安全關閉硬體資源...")
finally:
    picam2.stop()
    buzzer.off()
    alert_led.off()
    blk_pin.value = False 
    print("系統已安全關閉。")
