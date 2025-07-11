import os
import sys
import pyautogui as pgi
import tkinter as tk
from tkinter import messagebox

width, height = pgi.size()
def get_res_path():
    res = str(width) + 'x' + str(height)
    result = os.path.join(os.getcwd(), 'resources', res)
    return result

def get_img_path():
    res = str(width) + 'x' + str(height)
    result = './resources/' + res + '/'
    return result

def show_popup_folder_made():
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        "해상도 폴더 생성",
        "폴더가 생성되었습니다.\n\n필요한 이미지들을 폴더 안에 추가해주세요."
    )
    root.destroy()

def show_popup_folder_error():
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "해상도 폴더 생성",
        "경로를 만들지 못했습니다."
    )
    root.destroy()

def init_folder():
    res_path = get_res_path()
    if not os.path.exists(res_path):
        try:
            os.makedirs(res_path)
        except:
            show_popup_folder_error()
            sys.exit(0)
        else:
            show_popup_folder_made()