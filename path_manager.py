import os
import pyautogui as pgi

width, height = pgi.size()
def get_res_path():
    res = str(width) + 'x' + str(height)
    result = os.path.join(os.getcwd(), 'resources', res)
    return result

def get_img_path():
    res = str(width) + 'x' + str(height)
    result = './resources/' + res + '/'
    return result