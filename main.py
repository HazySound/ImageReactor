# 제작 : killsonic@naver.com 불닭@네이버
# 수정 : <HazySound>

import pyautogui as pgi
import pyscreeze
import time
import keyboard
import pydirectinput as pyd
import random
import autoemail
import path_manager as pm
import json
import sys
import os

width, height = pgi.size()  # 화면해상도 확인

img_path = pm.get_img_path()
res_path = pm.get_res_path()

is_crashed = False


def init_resources():
    # 폴더 체크 및 생성
    if not os.path.exists(res_path):
        try:
            os.makedirs(res_path)
            print("폴더가 생성되었습니다.")
            print("필요한 이미지들을 폴더 안에 추가해주세요.")
        except:
            print("경로를 만들지 못했습니다.")
            os.system('pause')
            sys.exit(0)

    # 텍스트 파일 체크 및 생성
    try:
        autoemail.init_txts()
    except:
        print("텍스트 파일 체크 및 생성에 문제가 발생하였습니다.")
        os.system('pause')
        sys.exit(0)

def import_img(file, conf=0.8):
    pyscreeze.USE_IMAGE_NOT_FOUND_EXCEPTION = False
    try:
        result = pgi.locateCenterOnScreen(file, confidence=conf)
        return result
    except pgi.ImageNotFoundException:
        return None

def imgclick(files, conf=0.8):  # 이미지가 있으면 클릭
    imgfile = import_img(files, conf)
    if imgfile == None:
        return
    else:
        x, y = imgfile
        x = x - random.randint(1, 20) + random.randint(1, 20)
        y = y - random.randint(1, 20)
        pgi.click(x, y)
        print("클릭함")

def spacepress(file):  # 이미지가 있으면 Space 누름
    imgfile = import_img(file)
    if imgfile == None:
        return
    else:
        print("space 눌림")
        pyd.keyDown("space")
        time.sleep(0.05)
        pyd.keyUp("space")

def skeypress(file):  # 이미지가 있으면 S 누름
    imgfile = import_img(file)
    if imgfile == None:
        return
    else:
        print("s 눌림")
        pyd.keyDown("s")
        time.sleep(0.05)
        pyd.keyUp("s")

def esckeypress(files):  # 이미지가 있으면 ESC 누름
    imgfile = import_img(files)
    if imgfile == None:
        return
    else:
        print("esc 눌림")
        pyd.keyDown("esc")
        time.sleep(0.05)
        pyd.keyUp("esc")

def client_crashed(img):
    imgfile = import_img(img, 0.9)

    if imgfile == None:  # 못 찾으면 이메일 발송
        autoemail.send_email()
        return True

    return False

def keep_awake():
    pyd.keyDown("s")
    time.sleep(0.05)
    pyd.keyUp("s")

def load_routine_from_json(path="./routine.json"):
    if not os.path.exists(path):
        return [], None

    with open(path, "r", encoding="utf-8") as f:
        raw_routine = json.load(f)

    # 하나만 있다고 가정
    client_item = next((item for item in raw_routine if item["action"] == "Client"), None)
    ordered_items = sorted(
        [item for item in raw_routine if item["action"] != "Client"],
        key=lambda x: x.get("order", 0)
    )
    return ordered_items, client_item

def execute_routine(routine_list):
    for item in routine_list:
        img_file = img_path + item["image"]
        action = item["action"]
        conf = item.get("conf", 0.8)

        if action == "click":
            imgclick(img_file, conf)
        elif action == "space":
            spacepress(img_file)
        elif action == "s":
            skeypress(img_file)
        elif action == "esc":
            esckeypress(img_file)


#  --- 메인 실행 파트 ---
# 리소스 초기화
init_resources()

# 루틴 추가 및 체크
routine_items, client_item = load_routine_from_json()
if not routine_items:
    print("❗ 루틴이 없습니다.")
    print("⛏ 먼저 config.exe에서 루틴을 설정한 후 다시 실행해주세요.")
    os.system("pause")
    sys.exit()

print('해상도 :', width, height)
print('\n* "F9" 반복 시작 / "ESC" 프로그램 종료')

try:
    while True:
        if keyboard.is_pressed('esc'):
            break

        elif keyboard.is_pressed('F9'):  # 반복 기능 만들어봄
            print('\n============반복 시작============')
            print('* "F12"꾹(중지 메시지가 뜰 때까지) 반복 중지')

            last_run = 0
            interval = 0.3  # 루틴 실행 간격 (초)
            while True:
                now = time.time()

                if client_item:
                    client_img_path = img_path + client_item["image"]
                    if os.path.exists(client_img_path) and client_crashed(client_img_path):
                        print("**아이콘 사라짐**")
                        print("절전 방지 모드로 진입합니다.")
                        print("절전 방지를 종료하고 초기 화면으로 돌아가려면 F12를 3초 가량 꾹 눌러주세요.")
                        is_crashed = True
                        break

                if keyboard.is_pressed('F12'):  # F12 중지
                    print('============중지됨============\n\n')
                    print('* "F9" 반복 시작 / "ESC" 프로그램 종료')
                    break

                # ⏱ 루틴 실행 간격 조절
                if now - last_run >= interval:
                    execute_routine(routine_items)
                    last_run = now

                time.sleep(0.01)  # CPU 부담 방지용

        elif is_crashed:
            keep_awake()  # 절전 방지 - 3초에 한 번씩 S키 누르기
            time.sleep(3)
            if keyboard.is_pressed('F12'):
                print('============중지됨============\n\n')
                print('* "F9" 작업 시작 / "ESC" 프로그램 종료')
                is_crashed = False

        time.sleep(0.1)  # CPU 부담 방지용
    os.system('pause')
except KeyboardInterrupt:
    print("프로그램을 종료합니다.")
    os.system('pause')
    sys.exit(0)


