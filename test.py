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
# 系統層級與絕對色彩矩陣 (精準對抗雙重反轉)
# ---------------------------------------------------------
import gpiozero.devices
gpiozero.devices.pin_factory = LGPIOFactory()

# BGR 絕對色盤 (傳入此數值，經過硬體反相後，螢幕將呈現您預期的顏色)
COLOR_RED    = (0, 0, 255)    # 螢幕將呈現：純紅
COLOR_GREEN  = (0, 255, 0)    # 螢幕將呈現：純綠
COLOR_BLUE   = (255, 0, 0)    # 螢幕將呈現：純藍
COLOR_YELLOW = (0, 255, 255)  # 螢幕將呈現：純黃
COLOR_WHITE  = (255, 255, 255)
COLOR_BLACK  = (0, 0, 0)

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

# ---------------------------------------------------------
# 硬體初始化
# ---------------------------------------------------------
sys.stdout.write('\033[2J\033[H')
print("正在初始化硬體資源...")

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

# 畫面正立且位於頂部
display = st7789.ST7789(
    spi, cs=None, dc=dc_pin, rst=rst_pin, 
    baudrate=64000000, width=240, height=320, rotation=180 
)

MODEL_PATH = "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk"
imx500 = IMX500(MODEL_PATH)
picam2 = Picamera2(imx500.camera_num if hasattr(imx500, 'camera_num') else 0)

config = picam2.create_video_configuration(main={"size": (640, 480), "format": "RGB888"})
picam2.configure(config)
picam2.start()

# ---------------------------------------------------------
# 非同步雙核心視覺引擎 (解決延遲與辨識遺失)
# ---------------------------------------------------------
ai_data = {"frame": None, "detections": []}
ai_lock = threading.Lock()

def vision_worker():
    """背景高速推論引擎，全速榨乾 NPU 效能"""
    while True:
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
            
            # 精準解析 NPU 輸出，與當下 frame 絕對同步
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
            
            # C 語言底層極速切片：上下翻轉、左右翻轉、RGB轉BGR
            frame_bgr_flipped = np.ascontiguousarray(frame[::-1, ::-1, ::-1])
            
            with ai_lock:
                ai_data["frame"] = frame_bgr_flipped
                ai_data["detections"] = current_detections
                
        except Exception:
            pass
        finally:
            # 【極度重要】釋放緩衝區，防呆防崩潰
            if req:
                req.release()

threading.Thread(target=vision_worker, daemon=True).start()

def trigger_alarm():
    alert_led.on()
    buzzer.value = 0.5 
    time.sleep(1)      
    buzzer.value = 0
    alert_led.off()

# ---------------------------------------------------------
# 主畫面與儀表板迴圈
# ---------------------------------------------------------
screen_on = True
fps_time = time.time()
frame_count = 0
fps = 0.0
last_ui_update = 0
last_log_time = 0

try:
    sys.stdout.write('\033[2J\033[H')
    
    while True:
        curr_time = time.time()
        
        # 從背景引擎拿取最新鮮的資料 (零延遲)
        with ai_lock:
            if ai_data["frame"] is None:
                continue
            frame_raw = ai_data["frame"].copy()
            detections = list(ai_data["detections"])
            
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

        # 處理日誌 (加入防洗版設計：每秒最多記錄一次)
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

        if screen_on:
            # 進行中心裁切 (X軸 140 到 500)
            frame_cropped = frame_raw[:, 140:500]
            
            img_cam = Image.fromarray(frame_cropped)
            img_cam = img_cam.resize((240, 320), Image.Resampling.NEAREST)
            draw = ImageDraw.Draw(img_cam)
            
            for det in detections:
                ymin, xmin, ymax, xmax = det['box']
                cls_id = det['category']
                score = det['conf']
                
                # 追蹤框 180 度反轉對位
                ymin, ymax = 1.0 - ymax, 1.0 - ymin
                xmin, xmax = 1.0 - xmax, 1.0 - xmin
                
                # 依目標指派專屬顏色
                if cls_id == 0:    # 人 (黃框)
                    color = COLOR_YELLOW
                elif cls_id == 15: # 貓 (綠框)
                    color = COLOR_GREEN
                elif cls_id == 16: # 狗 (藍框)
                    color = COLOR_BLUE
                else:              # 其他動物 (紅框)
                    color = COLOR_RED
                
                # 座標映射計算
                abs_x1, abs_x2 = xmin * 640, xmax * 640
                abs_y1, abs_y2 = ymin * 480, ymax * 480
                
                final_x1 = (abs_x1 - 140) * (240 / 360)
                final_x2 = (abs_x2 - 140) * (240 / 360)
                final_y1 = abs_y1 * (320 / 480)
                final_y2 = abs_y2 * (320 / 480)
                
                # 畫出極度顯眼的粗線框
                draw.rectangle(((final_x1, final_y1), (final_x2, final_y2)), outline=color, width=3)
                
                label_t = COCO_LABELS[cls_id]
                txt = f"{label_t[0] if supports_cjk and label_t[0] else label_t[1]} {int(score*100)}%"
                
                txt_bbox = font_id.getbbox(txt)
                draw.rectangle((final_x1, final_y1-18, final_x1+(txt_bbox[2]-txt_bbox[0])+4, final_y1), fill=color)
                draw.text((final_x1 + 2, final_y1 - 18), txt, fill=COLOR_BLACK, font=font_id)

            # 繪製頂部狀態列
            draw.rectangle((0, 0, 240, 22), fill=COLOR_BLACK)
            
            status_part1 = f"NPU | {env_data['temp']:.1f}°C | "
            status_part2 = f"TH:{max(1, encoder.steps)}%"
            
            # 畫出綠色的溫濕度
            draw.text((5, 2), status_part1, fill=COLOR_GREEN, font=font_main)
            
            # 畫出霸氣的紅色粗體門檻
            bbox = font_main.getbbox(status_part1)
            draw.text((5 + bbox[2], 2), status_part2, fill=COLOR_RED, font=font_main, stroke_width=1, stroke_fill=COLOR_RED)
            
            display_img = ImageOps.invert(img_cam)
            display.image(display_img)
        
        # 終端機 UI 更新 (降低頻率至 0.25秒，節省 CPU)
        if curr_time - last_ui_update > 0.25:
            last_ui_update = curr_time
            sys.stdout.write('\033[H') 
            print(f"==================================================")
            print(f" 🛡️ [CYBER-SENTRY PRO] 邊緣 AI 戰術儀表板")
            print(f" 晶片模式 : \033[36m異步雙核心引擎 (Asynchronous)\033[0m")
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
                print(f"  {log['time']:<8} | {log['label']:<8} | {log['conf']:>3}%    | {log['temp']:.1f}°C   ")
            
            for _ in range(10 - len(detection_log)):
                print("                                                  ")
            sys.stdout.flush()

        time.sleep(0.01)

except KeyboardInterrupt:
    sys.stdout.write('\n\033[2J\033[H')
    print("收到中斷訊號，正在安全關閉硬體資源...")
finally:
    picam2.stop()
    buzzer.off()
    alert_led.off()
    blk_pin.value = False 
    print("系統已安全關閉。")
