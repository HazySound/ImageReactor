# 제작 : killsonic@naver.com 불닭@네이버
# 수정 : <HazySound>

import pyautogui as pgi
import time
import keyboard
import pydirectinput as pyd
import random
import autoemail
import sys
import os

width, height = pgi.size()  # 화면해상도 확인
res = str(width) + 'x' + str(height)
img_path = './resources/' + res + '/'
res_path = os.path.join(os.getcwd(), 'resources', res)
print('해상도 :', width, height)

if not os.path.exists(res_path):
    try:
        os.makedirs(res_path)
    except:
        print("경로를 만들지 못했습니다.")


print('\n* "F9" 반복 시작 / "ESC" 프로그램 종료')

def import_img(file, conf=0.8):
    try:
        result = pgi.locateCenterOnScreen(file, confidence=conf)
        return result
    except pgi.ImageNotFoundException:
        return None



def imgclick(files, conf=0.8):  # 이미지 찾아서 클릭 함수
    imgfile = import_img(files, conf)
    if imgfile == None:
        #print("imgclick_못 찾음")
        return
    else:
        x, y = imgfile
        x = x - random.randint(1, 20) + random.randint(1, 20)
        y = y - random.randint(1, 20) + random.randint(1, 20)
        pgi.click(x, y)
        print("클릭함")


def spacepress(file):
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
        #print("skeypress_못 찾음")
        return
    else:
        print("s 눌림")
        pyd.keyDown("s")
        time.sleep(0.05)
        pyd.keyUp("s")


def esckeypress(files):  # 이미지가 있으면 S 누름
    imgfile = import_img(files)
    if imgfile == None:
        #print("esckeypress_못 찾음")
        return
    else:
        print("esc 눌림")
        pyd.keyDown("esc")
        time.sleep(0.05)
        pyd.keyUp("esc")

is_crashed = False
def client_crashed(img):
    imgfile = import_img(img, 0.9)

    if imgfile == None: #못 찾으면 이메일 발송
        autoemail.send_email()
        return True

    return False

def keep_awake():
    pyd.keyDown("s")
    time.sleep(0.05)
    pyd.keyUp("s")

def routine():
    print("루틴 시작")
    try:
        imgclick(img_path + 'click1.png', 0.98)  # 유사치 98% 이상 클릭 1
        skeypress(img_path + 's1.png')  # s키 1
        imgclick(img_path + 'click2.png')  # 클릭 2
        imgclick(img_path + 'click3.png')  # 클릭 3
        spacepress(img_path + 'space1.png')  # 스페이스바 1
        spacepress(img_path + 'space2.png')  # 스페이스바 2
        spacepress(img_path + 'space3.png')  # 스페이스바 3
        imgclick(img_path + 'click4.png')  # 클릭 4
        esckeypress(img_path + 'esc.png')  # esc
        skeypress(img_path + 's2.png')  # s키 2
    except Exception as e:
        print("이미지 파일이 있는지 확인해주세요 : ", e)

try:
    while True:
        if keyboard.is_pressed('esc'):
            break

        elif keyboard.is_pressed('F9'):  # 반복 기능 만들어봄
            print('\n============반복 시작============')
            print('* "F12"꾹(중지 메시지가 뜰 때까지) 반복 중지')

            while True:
                time.sleep(0.001)
                if client_crashed(img_path + 'icon.png'):
                    print("**아이콘 사라짐**")
                    print("절전 방지 모드로 진입합니다.")
                    print("절전 방지를 종료하고 초기 화면으로 돌아가려면 F12를 3초 가량 꾹 눌러주세요.")
                    is_crashed = True
                    break

                if keyboard.is_pressed('F12'):  # F12 중지
                    print('============중지됨============\n\n')
                    print('* "F9" 반복 시작 / "ESC" 프로그램 종료')
                    break
                else:
                    routine()

        elif is_crashed:
            keep_awake() #절전 방지 - 3초에 한 번씩 S키 누르기
            time.sleep(3)
            if keyboard.is_pressed('F12'):
                print('============중지됨============\n\n')
                print('* "F9" 작업 시작 / "ESC" 프로그램 종료')
                is_crashed = False

        time.sleep(0.001)
    os.system('pause')
except KeyboardInterrupt:
    print("프로그램을 종료합니다.")
    os.system('pause')
    sys.exit(0)


