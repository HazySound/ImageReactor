# 전체 코드 재작성: 기능 통합 및 최적화

import tkinter as tk
from tkinter import messagebox, Scrollbar, Canvas
from tkinter import Toplevel, Label, Button
from PIL import Image, ImageTk
import os
import sys
import json
import path_manager as pm
import re
from lock_utils import create_lock, remove_lock, check_stale_lock


class ToolTip:
    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self.delay = delay  # 지연 시간 (밀리초)
        self.after_id = None

        widget.bind("<Enter>", self.schedule)
        widget.bind("<Leave>", self.cancel)
        widget.bind("<Motion>", self.move)

    def schedule(self, event=None):
        self.cancel()
        self.after_id = self.widget.after(self.delay, self.show)

    def cancel(self, event=None):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        self.hide()

    def move(self, event):
        # 움직임에도 툴팁 유지되도록 하기 위해 아무 처리도 안 함
        pass

    def show(self):
        if self.tip_window or not self.text:
            return
        x, y = self.widget.winfo_pointerxy()
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x+10}+{y+10}")
        label = tk.Label(
            tw, text=self.text,
            background="#ffffe0", relief="solid", borderwidth=1,
            font=("맑은 고딕", 9)
        )
        label.pack(ipadx=4, ipady=2)

    def hide(self):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


# ---------------------
# ✅ 전역 변수 정의 구역
# ---------------------

ROUTINE_FILE = "routine.json"
THUMBNAIL_SIZE = (100, 100)
routine = []
image_list = []
image_buttons = {}  # 버튼 참조용
selected_image = None
highlight_line = None

# 루틴 변경 여부 확인 플래그
routine_modified = False

image_folder = pm.get_img_path()

# 폴더 경로 확인 및 생성
pm.init_folder()

root = tk.Tk()
root.title("루틴 설정")

# main 프로그램 실행 여부 확인
if os.path.exists("routine.lock"):
    if check_stale_lock():
        messagebox.showwarning("실행 중지", "다른 인스턴스가 실행 중입니다.")
        sys.exit()

# 화면 중앙 배치
window_width = 900
window_height = 800
screen_width = root.winfo_screenwidth()
screen_height = root.winfo_screenheight()
x = int((screen_width - window_width) / 2)
y = int((screen_height - window_height) / 2)
root.geometry(f"{window_width}x{window_height}+{x}+{y}")

root.resizable(False, False)

selected_image_var = tk.StringVar()
conf_var = tk.DoubleVar(value=0.8)  # 기본값은 일반 루틴 기준


# 다이얼로그 중앙 출력 관련 함수
def center_window(win, width, height):
    win.update_idletasks()
    screen_width = win.winfo_screenwidth()
    screen_height = win.winfo_screenheight()
    x = int((screen_width - width) / 2)
    y = int((screen_height - height) / 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


# action_var 관련 함수 및 변수
def load_action_definitions():
    default = [
        {"label": "click", "value": "click"},
        {"label": "space", "value": "space"},
        {"label": "s", "value": "s"},
        {"label": "esc", "value": "esc"},
        {"label": "Client", "value": "Client"}
    ]

    if not os.path.exists("action_definitions.json"):
        return default

    try:
        with open("action_definitions.json", "r", encoding="utf-8") as f:
            loaded = json.load(f)

        # 기본 동작이 빠져 있으면 추가
        existing_values = {item["value"] for item in loaded}
        for d in default:
            if d["value"] not in existing_values:
                loaded.insert(0, d)

        return loaded
    except Exception as e:
        print("action_definitions.json 로드 실패:", e)
        return default


action_var = tk.StringVar(value="click")
action_objects = load_action_definitions()

# 동작 동적 추가/삭제 관련 상수 및 함수 정의
FIXED_ACTIONS = ["click", "space", "s", "esc", "Client"]  # 삭제 불가능한 동작 목록

selected_action_label = tk.StringVar()
selected_action_label.set("click")  # 초기값


def set_client_action_menu_state(enable=True):
    for i, obj in enumerate(action_objects):
        if obj["value"] == "Client":
            state = "normal" if enable else "disabled"
            action_menu_btn.menu.entryconfig(i, state=state)


def save_action_definitions():
    with open("action_definitions.json", "w", encoding="utf-8") as f:
        json.dump(action_objects, f, ensure_ascii=False, indent=2)


def update_action_menu():
    menu = action_menu_btn.menu
    menu.delete(0, "end")

    for obj in action_objects:
        menu.add_command(
            label=obj["label"],
            command=lambda v=obj["value"]: on_action_selected(v)
        )

    # 선택된 값 기준으로 표시 텍스트(label) 동기화
    current_value = action_var.get()
    match = next((o for o in action_objects if o["value"] == current_value), None)
    if match:
        selected_action_label.set(match["label"])


selected_routine_index = None

# ---------- GUI ----------
# 이미지 목록
top_frame = tk.Frame(root)
top_frame.pack(pady=10, fill="x")
tk.Label(top_frame, text="🖼 이미지 파일 목록", font=("맑은 고딕", 13, "bold")).pack()
tk.Label(top_frame, text="※ 한글이 포함된 이미지 파일은 표시되지 않습니다", font=("맑은 고딕", 9), fg="gray").pack()


canvas = Canvas(top_frame, height=220, width=820)
scroll_y = Scrollbar(top_frame, orient="vertical", command=canvas.yview)
frame_thumbs = tk.Frame(canvas)
frame_thumbs.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
canvas.create_window((410, 0), window=frame_thumbs, anchor='n')
canvas.configure(yscrollcommand=scroll_y.set)
canvas.pack(side="top", fill="both", expand=True)
scroll_y.pack(side="right", fill="y")


def contains_korean(text):
    return bool(re.search(r'[ㄱ-ㅎㅏ-ㅣ가-힣]', text))


row, col = 0, 0
for file_name in os.listdir(image_folder):
    if file_name.lower().endswith(".png"):
        if contains_korean(file_name):
            print(f"[제외됨] 한글 포함 파일: {file_name}")
            continue  # 한글 포함 파일은 무시

        image_list.append(file_name)
        path = os.path.join(image_folder, file_name)
        img = Image.open(path)
        img.thumbnail(THUMBNAIL_SIZE)
        tk_img = ImageTk.PhotoImage(img)
        btn = tk.Button(frame_thumbs, image=tk_img, command=lambda n=file_name: select_image(n))
        btn.image = tk_img
        btn.grid(row=row, column=col, padx=5, pady=5)
        image_buttons[file_name] = btn
        col += 1
        if col >= 5:
            col = 0
            row += 1


# Main Frame
main_frame = tk.Frame(root)
main_frame.pack(padx=20, fill="both", expand=True)


# Left Frame
left_frame = tk.Frame(main_frame)
left_frame.pack(side="left", anchor="n", padx=(0, 40))

# 선택 정보
tk.Label(left_frame, text="선택된 이미지:", font=("맑은 고딕", 11)).pack(anchor="w")
selected_image_thumb = tk.Label(left_frame)
selected_image_thumb.pack(anchor="w", pady=(0, 5))
selected_label = tk.Label(left_frame, text="없음", font=("맑은 고딕", 11, "bold"), anchor="w", width=40)
selected_label.pack(anchor="w")


# 동작 관련 함수 정의 및 UI 설정
def show_label_input_dialog(raw_action):
    MAX_LENGTH = 20

    def on_confirm():
        label = entry_var.get().strip()

        if not label:
            messagebox.showwarning("입력 오류", "표시 이름을 입력해주세요.", parent=root)
            return

        if len(label) > MAX_LENGTH:
            messagebox.showwarning("입력 오류", f"이름이 너무 깁니다. (최대 {MAX_LENGTH}자)", parent=root)
            return

        if any(obj["value"] == raw_action for obj in action_objects):
            messagebox.showinfo("중복 동작", f"이미 존재하는 동작입니다: {label}", parent=root)
            return

        action_objects.append({"label": label, "value": raw_action})
        update_action_menu()
        action_var.set(raw_action)
        save_action_labels()
        messagebox.showinfo("동작 추가됨", f"'{label}' 동작이 추가되었습니다.", parent=root)
        dialog.grab_release()
        dialog.destroy()
        
        # 추가된 동작 저장
        save_action_definitions()

    def on_cancel():
        dialog.grab_release()
        dialog.destroy()

    dialog = Toplevel(root)
    dialog.title("이름 입력")
    center_window(dialog, 320, 140)
    dialog.resizable(False, False)
    dialog.grab_set()  # modal 설정
    dialog.focus_force()

    Label(dialog, text="이 동작의 표시 이름을 입력하세요", font=("맑은 고딕", 11, "bold")).pack(pady=(15, 5))

    entry_var = tk.StringVar(value=raw_action)
    entry = tk.Entry(dialog, textvariable=entry_var, font=("맑은 고딕", 11), width=25)
    entry.pack(pady=(0, 10))
    entry.focus_set()

    btn_frame = tk.Frame(dialog)
    btn_frame.pack()
    Button(btn_frame, text="확인", width=10, command=on_confirm).grid(row=0, column=0, padx=10)
    Button(btn_frame, text="취소", width=10, command=on_cancel).grid(row=0, column=1, padx=10)


def show_key_capture_dialog():
    if getattr(root, "_key_dialog_open", False):
        return
    root._key_dialog_open = True

    MAX_LENGTH = 20
    captured_keys = []
    pressed_keys = []
    last_valid_combo = []

    def normalize_key(key):
        return {
            "Control_L": "Ctrl", "Control_R": "Ctrl",
            "Shift_L": "Shift", "Shift_R": "Shift",
            "Alt_L": "Alt", "Alt_R": "Alt",
            "Return": "Enter",
            "Escape": "Esc",
            "space": "Space"
        }.get(key, key.capitalize())

    def on_key_press(event):
        key = normalize_key(event.keysym)
        if key not in pressed_keys:
            pressed_keys.append(key)

    def on_key_release(event):
        nonlocal last_valid_combo

        key = normalize_key(event.keysym)

        if key == "Esc":
            pressed_keys.clear()
            captured_keys.clear()
            last_valid_combo = []
            label_var.set("입력 대기 중...")
            return

        if key in pressed_keys:
            pressed_keys.remove(key)

        current_combo = pressed_keys + [key] if key not in pressed_keys else pressed_keys

        if len(current_combo) > 1 and not last_valid_combo:
            last_valid_combo = current_combo.copy()

        if not pressed_keys:
            action = '+'.join(last_valid_combo) if last_valid_combo else key
            captured_keys.clear()
            captured_keys.append(action)
            label_var.set(action)
            last_valid_combo = []

    def on_decide():
        if not captured_keys:
            messagebox.showwarning("입력 오류", "입력된 키가 없습니다.", parent=root)
            return

        dialog.grab_release()
        dialog.destroy()
        show_label_input_dialog(captured_keys[0])  # 키 입력 후 라벨 입력 창 호출

        # on_cancel() 끝 또는 on_decide() 끝
        root._key_dialog_open = False

    def on_cancel():
        dialog.grab_release()
        dialog.destroy()

        # on_cancel() 끝 또는 on_decide() 끝
        root._key_dialog_open = False

    dialog = Toplevel(root)
    dialog.title("키 입력 감지")
    center_window(dialog, 340, 170)
    dialog.resizable(False, False)
    dialog.grab_set()  # modal 설정
    dialog.focus_force()

    Label(dialog, text="입력할 키를 직접 눌러주세요!", font=("맑은 고딕", 11, "bold")).pack(pady=(10, 5))

    label_var = tk.StringVar(value="입력 대기 중...")
    Label(dialog, textvariable=label_var, font=("맑은 고딕", 12), fg="blue").pack()

    Label(dialog,
          text="※ ESC: 입력 초기화\n※ 모든 키에서 손을 뗐을 때 입력 확정",
          font=("맑은 고딕", 9), fg="gray").pack(pady=(5, 10))

    btn_frame = tk.Frame(dialog)
    btn_frame.pack()

    Button(btn_frame, text="결정", width=10, command=on_decide).grid(row=0, column=0, padx=10)
    Button(btn_frame, text="취소", width=10, command=on_cancel).grid(row=0, column=1, padx=10)

    dialog.bind("<KeyPress>", on_key_press)
    dialog.bind("<KeyRelease>", on_key_release)


# 동작 삭제 함수
def delete_current_action():
    action = action_var.get()

    if action in FIXED_ACTIONS:
        messagebox.showwarning("삭제 불가", f"'{action}' 동작은 삭제할 수 없습니다.", parent=root)
        return

    # 현재 선택된 label을 찾기 (UI에 보여줄 이름용)
    match = next((obj for obj in action_objects if obj["value"] == action), None)
    label = match["label"] if match else action

    if messagebox.askyesno("동작 삭제", f"'{label}' 동작을 삭제하시겠습니까?", parent=root):
        # action_objects에서 해당 항목 제거
        action_objects[:] = [obj for obj in action_objects if obj["value"] != action]
        update_action_menu()

        # ✅ 삭제 직후 저장
        save_action_definitions()

        # 기본값 "click"이 남아 있으면 선택, 없으면 첫 항목으로 fallback
        if any(obj["value"] == "click" for obj in action_objects):
            action_var.set("click")
        else:
            if action_objects:
                action_var.set(action_objects[0]["value"])

        messagebox.showinfo("동작 삭제됨", f"'{label}' 동작이 삭제되었습니다.", parent=root)


action_top = tk.Frame(left_frame)
action_top.pack(anchor="w", pady=(20, 10))

tk.Label(action_top, text="입력 동작 선택", font=("맑은 고딕", 11)).grid(row=0, column=0, sticky="w", padx=(0, 10))

add_action_btn = tk.Button(action_top, text="추가", width=5, command=show_key_capture_dialog)
add_action_btn.grid(row=0, column=1, padx=(20, 15))

delete_action_btn = tk.Button(action_top, text="삭제", width=5, command=lambda: delete_current_action())
delete_action_btn.grid(row=0, column=2)

selected_action_label = tk.StringVar()
selected_action_label.set("click")  # 기본 표시값

# 입력 동작 선택 메뉴 드롭다운 UI
action_menu_btn = tk.Menubutton(left_frame, textvariable=selected_action_label, relief="raised", width=20)
action_menu_btn.menu = tk.Menu(action_menu_btn, tearoff=0)
action_menu_btn["menu"] = action_menu_btn.menu


def on_action_selected(value):
    action_var.set(value)
    match = next((obj for obj in action_objects if obj["value"] == value), None)
    if match:
        selected_action_label.set(match["label"])


# 메뉴 초기 구성
for obj in action_objects:
    action_menu_btn.menu.add_command(label=obj["label"], command=lambda v=obj["value"]: on_action_selected(v))

action_menu_btn.pack(anchor="w")

action_display_label = tk.Label(left_frame, text="선택된 동작: click", font=("맑은 고딕", 10), anchor="w")
action_display_label.pack(anchor="w", pady=(0, 10))

# conf 설정 라벨
tk.Label(left_frame, text="인식 유사도", font=("맑은 고딕", 11)).pack(anchor="w", pady=(20, 10))

# 슬라이더 + 숫자 입력 프레임
conf_frame = tk.Frame(left_frame)
conf_frame.pack(anchor="w", pady=(0, 20))  # 버튼들과 충분한 간격 확보

# 슬라이더
conf_slider = tk.Scale(conf_frame, from_=0.8, to=0.99, resolution=0.01,
                       orient="horizontal", variable=conf_var, length=150)
conf_slider.grid(row=0, column=0, sticky='w')

# 숫자 입력창
conf_entry = tk.Entry(conf_frame, width=5, font=("맑은 고딕", 11))
conf_entry.grid(row=0, column=1, padx=(10, 0), sticky='w', ipady=3)
conf_entry.insert(0, f"{conf_var.get():.2f}")



# Right Frame - 루틴 미리보기 목록
right_frame = tk.Frame(main_frame)
right_frame.pack(side="left", anchor="n")
tk.Label(right_frame, text="📋 현재 루틴 미리보기", font=("맑은 고딕", 12, "bold")).pack(anchor="w")
tk.Label(right_frame, text="※ 항목을 우클릭하면 삭제됩니다", font=("맑은 고딕", 9), fg="gray").pack(anchor="w", pady=(0, 0))
tk.Label(right_frame, text="※ 드래그하여 순서를 변경할 수 있습니다", font=("맑은 고딕", 9), fg="gray").pack(anchor="w", pady=(0, 5))
preview_canvas_wrapper = tk.Frame(right_frame, height=260)
preview_canvas_wrapper.pack(anchor="w")
preview_scroll = Scrollbar(preview_canvas_wrapper, orient="vertical")
preview_scroll.pack(side="right", fill="y")
preview_canvas = Canvas(preview_canvas_wrapper, yscrollcommand=preview_scroll.set, height=260, width=420)
preview_scroll.config(command=preview_canvas.yview)
preview_canvas.pack(side="left")
preview_frame = tk.Frame(preview_canvas)
preview_canvas.create_window((0, 0), window=preview_frame, anchor="nw")
preview_frame.bind("<Configure>", lambda e: preview_canvas.configure(scrollregion=preview_canvas.bbox("all")))



# Client 전용 표시 영역
client_frame = tk.Frame(right_frame)
client_frame.pack(anchor="w", pady=(10, 0))
tk.Label(client_frame, text="🟦 클라이언트 감지용 아이콘", font=("맑은 고딕", 11, "bold"), anchor="w").pack(anchor="w")
client_display = tk.Frame(client_frame)
client_display.pack(anchor="w")


def on_action_change(*args):
    raw_value = action_var.get()

    # 현재 선택된 label 찾아서 표시
    match = next((obj for obj in action_objects if obj["value"] == raw_value), None)
    if match:
        action_display_label.config(text=f"선택된 동작: {match['label']}")
        selected_action_label.set(match["label"])
    else:
        action_display_label.config(text=f"선택된 동작: {raw_value}")
        selected_action_label.set(raw_value)

    # 삭제 버튼 활성/비활성 제어
    if raw_value in FIXED_ACTIONS:
        delete_action_btn.config(state="disabled")
    else:
        delete_action_btn.config(state="normal")

    # 현재 선택된 이미지 이름
    selected = selected_image_var.get()

    # 루틴에 선택된 이미지가 있는지 확인
    match_routine = next((r for r in routine if r['image'] == selected), None)

    default_conf = 0.9 if raw_value == "Client" else 0.8

    if match_routine:
        if abs(match_routine.get("conf", default_conf) - default_conf) < 1e-6:
            conf_var.set(default_conf)
            sync_conf_entry()
    else:
        conf_var.set(default_conf)
        sync_conf_entry()


def on_mousewheel(event):
    # 현재 스크롤 가능한지 먼저 판단
    scroll_region = preview_canvas.bbox("all")
    canvas_height = preview_canvas.winfo_height()

    if scroll_region:
        content_height = scroll_region[3] - scroll_region[1]
        if content_height > canvas_height:
            preview_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


def update_client_preview():
    for widget in client_display.winfo_children():
        widget.destroy()

    for item in routine:
        if item['action'] == "Client":
            path = os.path.join(image_folder, item['image'])
            if not os.path.exists(path):
                return
            img = Image.open(path)
            img.thumbnail((50, 50))
            tk_img = ImageTk.PhotoImage(img)

            # 이미지 라벨
            img_label = tk.Label(client_display, image=tk_img, cursor="question_arrow")
            img_label.image = tk_img
            img_label.grid(row=0, column=0, padx=5, pady=5)

            # 🔵 하이라이트 효과 on hover (이미지)
            img_label.bind("<Enter>", lambda e, lbl=img_label: lbl.config(bg="#d0eaff"))
            img_label.bind("<Leave>", lambda e, lbl=img_label: lbl.config(bg=client_display.cget("bg")))

            # 🔵 툴팁 표시 (이미지)
            ToolTip(img_label, "우클릭 시 해제됩니다")

            # 🔵 삭제 바인딩 (이미지)
            img_label.bind("<Button-3>", lambda e, i=routine.index(item): delete_routine(i))

            # 이미지 이름 라벨
            text_label = tk.Label(client_display, text=item['image'], font=("맑은 고딕", 10), anchor="w")
            text_label.grid(row=0, column=1, sticky="w")

            # 🔵 툴팁 표시 (텍스트)
            ToolTip(text_label, "우클릭 시 삭제됩니다")

            # ✅ 하이라이트 정상 작동 버전 (텍스트)
            text_label.bind("<Enter>", lambda e, lbl=text_label: lbl.config(bg="#d0eaff"))
            text_label.bind("<Leave>", lambda e, lbl=text_label: lbl.config(bg=client_display.cget("bg")))

            # 🔵 삭제 바인딩 (텍스트)
            text_label.bind("<Button-3>", lambda e, i=routine.index(item): delete_routine(i))


def update_preview():
    update_client_preview()

    # 기존 항목 제거
    for widget in preview_frame.winfo_children():
        widget.destroy()

    # Client 제외하고 정렬
    non_client_routine = [r for r in routine if r['action'] != "Client"]
    sorted_routine = sorted(non_client_routine, key=lambda x: x['order'])

    for idx, item in enumerate(sorted_routine):
        path = os.path.join(image_folder, item['image'])
        if not os.path.exists(path):
            continue
        img = Image.open(path)
        img.thumbnail(THUMBNAIL_SIZE)
        tk_img = ImageTk.PhotoImage(img)

        # 이미지 라벨
        img_label = tk.Label(preview_frame, image=tk_img)
        img_label.image = tk_img
        img_label.grid(row=idx, column=0, padx=5, pady=5)
        img_label.bind("<MouseWheel>", on_mousewheel)  # 🛠 휠 스크롤 바인딩

        # 텍스트 라벨
        text = f"{idx+1:<3} | {item['image']:<25} → {item['action']}"
        lbl = tk.Label(preview_frame, text=text, font=("맑은 고딕", 10), anchor="w", width=40)
        lbl.grid(row=idx, column=1, sticky='w')
        lbl.bind("<MouseWheel>", on_mousewheel)  # 🛠 휠 스크롤 바인딩

        # 클릭 및 드래그 동작 바인딩
        lbl.bind("<Button-1>", lambda e, img=item['image']: select_from_routine_by_image(img))
        lbl.bind("<B1-Motion>", lambda e: drag_motion(e))
        lbl.bind("<ButtonRelease-1>", lambda e, img=item['image']: drag_release_by_image(img))
        lbl.bind("<Button-3>", lambda e, img=item['image']: delete_routine_by_image(img))

    # 🛠 스크롤 상태 계산
    preview_canvas.update_idletasks()
    scroll_region = preview_canvas.bbox("all")
    canvas_height = preview_canvas.winfo_height()

    if scroll_region:
        content_height = scroll_region[3] - scroll_region[1]

        # 콘텐츠가 작거나 루틴 수가 적으면 yview 초기화
        if content_height <= canvas_height or len(non_client_routine) <= 1:
            preview_canvas.yview_moveto(0)

        # 🛠 스크롤 범위 강제 재설정 (스크롤 방향 오류 방지)
        preview_canvas.configure(scrollregion=scroll_region)


def select_image(name):
    global selected_image
    selected_image = name
    selected_image_var.set(name)
    selected_label.config(text=name)

    for img_name, btn in image_buttons.items():
        if img_name in [r['image'] for r in routine]:
            btn.config(relief="sunken")
        else:
            btn.config(relief="raised")

    if name in image_buttons:
        image_buttons[name].config(relief="solid", bd=3)

    img_path = os.path.join(image_folder, name)
    if os.path.exists(img_path):
        img = Image.open(img_path)
        img.thumbnail((64, 64))
        tk_img = ImageTk.PhotoImage(img)
        selected_image_thumb.config(image=tk_img)
        selected_image_thumb.image = tk_img

    # 루틴에서 해당 이미지 검색
    match = next((r for r in routine if r['image'] == name), None)
    if match:
        action_var.set(match['action'])
        conf_var.set(match['conf'])  # ← 루틴에서 conf 값 로드
    else:
        action_var.set("click")
        conf_var.set(0.8 if action_var.get() != "Client" else 0.9)  # ← 새 이미지 기본값

    sync_conf_entry()  # ← 슬라이더와 입력창 동기화

    add_btn.config(state="normal")


def select_from_routine(index):
    global selected_routine_index
    selected_routine_index = index
    for child in preview_frame.winfo_children():
        if isinstance(child, tk.Label):
            child.config(bg=child.master.cget("bg"))
    label_widgets = [w for w in preview_frame.winfo_children() if isinstance(w, tk.Label)]
    if 0 <= index * 2 + 1 < len(label_widgets):
        label_widgets[index * 2 + 1].config(bg="lightblue")

    item = routine[index]
    select_image(item['image'])

    # Client 중복 방지 처리
    set_client_action_menu_state(
        enable=not any(r['action'] == "Client" and r['image'] != item['image'] for r in routine)
    )


def select_from_routine_by_image(image_name):
    global selected_routine_index

    selected_routine_index = next((i for i, r in enumerate(routine) if r['image'] == image_name), None)
    if selected_routine_index is None:
        return

    non_client_routine = sorted(
        [r for r in routine if r['action'] != "Client"],
        key=lambda x: x['order']
    )
    index_in_view = next((i for i, r in enumerate(non_client_routine) if r['image'] == image_name), None)

    for child in preview_frame.winfo_children():
        if isinstance(child, tk.Label):
            child.config(bg=child.master.cget("bg"))

    if index_in_view is not None:
        label_widgets = [w for w in preview_frame.winfo_children() if isinstance(w, tk.Label)]
        if 0 <= index_in_view * 2 + 1 < len(label_widgets):
            label_widgets[index_in_view * 2 + 1].config(bg="lightblue")

    select_image(image_name)

    for i, obj in enumerate(action_objects):
        if obj["value"] == "Client":
            if any(r['action'] == "Client" and r['image'] != image_name for r in routine):
                action_menu_btn.menu.entryconfig(i, state="disabled")
            else:
                action_menu_btn.menu.entryconfig(i, state="normal")


def update_client_action_menu():
    set_client_action_menu_state(enable=not any(r['action'] == "Client" for r in routine))


def update_save_button_state():
    if routine:
        save_btn.config(state="normal")
    else:
        save_btn.config(state="disabled")


def sync_conf_slider(*args):
    try:
        value = float(conf_entry.get())
        # 자동 범위 보정
        if value < 0.8:
            value = 0.8
        elif value > 0.99:
            value = 0.99
        conf_var.set(round(value, 2))
    except ValueError:
        pass  # 잘못된 입력 무시


def sync_conf_entry(*args):
    conf_entry.delete(0, tk.END)
    conf_entry.insert(0, f"{conf_var.get():.2f}")


def add_routine():
    global selected_image, routine_modified
    if not selected_image:
        return

    action = action_var.get()
    new_conf = round(conf_var.get(), 2)  # ← 현재 conf 슬라이더 값 가져오기

    existing = next((r for r in routine if r['image'] == selected_image), None)

    if action == "Client":
        # 이미 다른 이미지가 Client로 설정되어 있으면 추가 불가
        if any(r['action'] == "Client" and r['image'] != selected_image for r in routine):
            messagebox.showwarning("중복 불가", "이미 Client로 설정된 항목이 존재합니다.", parent=root)
            return

    if existing:
        if existing['action'] != "Client" and action == "Client":
            # 일반 → Client : 기존 항목 제거, 순서 재정렬, Client로 재추가
            routine.remove(existing)
            non_client = [r for r in routine if r['action'] != "Client"]
            for i, r in enumerate(non_client):
                r['order'] = i
            routine.append({"image": selected_image, "action": "Client", "conf": new_conf})
            messagebox.showinfo("변경됨", f"{selected_image}가 Client 항목으로 변경되었습니다.", parent=root)
        elif existing['action'] == "Client" and action != "Client":
            # Client → 일반 : order 부여 후 상태 변경
            existing['action'] = action
            existing['order'] = len([r for r in routine if r['action'] != "Client"])
            existing['conf'] = new_conf
            messagebox.showinfo("변경됨", f"{selected_image}가 일반 루틴 항목으로 변경되었습니다.", parent=root)
        else:
            # 일반 항목의 동작만 수정
            existing['action'] = action
            existing['conf'] = new_conf
            messagebox.showinfo("수정됨", f"{selected_image}의 동작이 수정되었습니다.", parent=root)
    else:
        # 새 항목 추가
        if action == "Client":
            routine.append({"image": selected_image, "action": "Client", "conf": new_conf})
        else:
            routine.append({
                "image": selected_image,
                "action": action,
                "order": len([r for r in routine if r['action'] != "Client"]),
                "conf": new_conf
            })
        image_buttons[selected_image].config(state="disabled")
        messagebox.showinfo("추가됨", f"{selected_image}가 루틴에 추가되었습니다.", parent=root)

    update_preview()
    update_client_action_menu()

    # UI 초기화
    add_btn.config(state="disabled")
    selected_image_var.set("")
    selected_label.config(text="없음")
    selected_image_thumb.config(image="")
    selected_image_thumb.image = None
    update_save_button_state()

    update_action_menu()  # 메뉴 라벨 동기화
    on_action_change()  # 현재 선택 반영

    routine_modified = True


def delete_routine(index):
    global routine_modified

    image = routine[index]['image']
    routine.pop(index)
    for i, r in enumerate(routine):
        r['order'] = i
    if image in image_buttons:
        image_buttons[image].config(state="normal")

    update_client_action_menu()

    update_preview()
    update_save_button_state()

    routine_modified = True


def delete_routine_by_image(image_name):
    global routine_modified

    index = next((i for i, r in enumerate(routine) if r['image'] == image_name), None)
    if index is not None:
        delete_routine(index)
        routine_modified = True  # 루틴 변경됨


def save_routine():
    global routine_modified

    # 잠금 파일 생성
    create_lock()

    try:
        with open(ROUTINE_FILE, "w", encoding="utf-8") as f:
            json.dump(routine, f, indent=2)
        messagebox.showinfo("저장 완료", "routine.json 파일로 저장되었습니다!", parent=root)
        routine_modified = False
    finally:
        # 저장 후 잠금 해제
        remove_lock()


def load_routine():
    global routine, routine_modified
    removed_images = []

    if os.path.exists(ROUTINE_FILE):
        try:
            with open(ROUTINE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            valid_routine = []
            for item in loaded:
                if "conf" not in item:
                    item["conf"] = 0.9 if item["action"] == "Client" else 0.8

                image_path = os.path.join(image_folder, item['image'])
                if os.path.exists(image_path):
                    valid_routine.append(item)
                else:
                    print(f"[제외됨] 존재하지 않는 이미지로 저장된 항목 제거됨: {item['image']}")
                    removed_images.append(item['image'])

            # json과 실제 이미지 간 불일치가 있었던 경우
            if removed_images:
                routine_modified = True  # 자동 변경이므로 저장 여부를 묻도록 함
                removed_list = "\n- " + "\n- ".join(removed_images)
                messagebox.showinfo(
                    "루틴 일부 항목 제거됨",
                    f"이미지 파일의 이름 변경 등의 이유로\n저장된 루틴 목록에서 다음 항목이 제거되었습니다:{removed_list}",
                    parent=root
                )

            routine = valid_routine

            for item in routine:
                if item['image'] in image_buttons:
                    image_buttons[item['image']].config(state="disabled")

            update_preview()
            update_client_action_menu()

        except Exception as e:
            messagebox.showerror("로드 실패", f"routine.json 파일을 불러오는 중 오류 발생:\n{e}", parent=root)

    update_save_button_state()


# 버튼 프레임
btn_frame = tk.Frame(left_frame)
btn_frame.pack(anchor="w")

# 추가/수정 버튼
add_btn = tk.Button(btn_frame, text="루틴 추가/수정", command=add_routine, state="disabled", width=12, height=2)
add_btn.grid(row=0, column=0, padx=(10, 5))   # 왼쪽 버튼은 좌측 여백 줄임

# 루틴저장 버튼
save_btn = tk.Button(btn_frame, text="루틴 저장", command=save_routine, state="disabled", width=12, height=2)
save_btn.grid(row=0, column=1, padx=(40, 0))  # 오른쪽 버튼은 왼쪽 여백 늘림


def drag_motion(event):
    global highlight_line
    labels = [w for w in preview_frame.winfo_children() if isinstance(w, tk.Label)]
    mouse_y = event.y_root
    target_row = None

    for i, label in enumerate(labels):
        top = label.winfo_rooty()
        bottom = top + label.winfo_height()
        if top <= mouse_y <= bottom:
            target_row = i // 2
            break

    # 🔽 자동 스크롤 조건 처리
    canvas_top = preview_canvas.winfo_rooty()
    canvas_bottom = canvas_top + preview_canvas.winfo_height()

    scroll_region = preview_canvas.bbox("all")
    if scroll_region:
        _, region_top, _, region_bottom = scroll_region
        if mouse_y < canvas_top + 20 and preview_canvas.canvasy(0) > region_top:
            preview_canvas.yview_scroll(-1, "units")  # 위로
        elif mouse_y > canvas_bottom - 20 and preview_canvas.canvasy(preview_canvas.winfo_height()) < region_bottom:
            preview_canvas.yview_scroll(1, "units")   # 아래로

    # 🔴 강조선 표시
    if target_row is not None:
        if highlight_line:
            highlight_line.destroy()
        if 0 <= target_row <= len([r for r in routine if r['action'] != "Client"]):
            highlight_line = tk.Frame(preview_frame, bg='red', height=2)
            highlight_line.grid(row=target_row, column=0, columnspan=2, sticky='ew')


def drag_release(index):
    global highlight_line
    if highlight_line:
        row = highlight_line.grid_info().get("row")
        highlight_line.destroy()
        highlight_line = None
        if 0 <= row < len(routine):
            routine.insert(row, routine.pop(index))
            for i, r in enumerate(routine):
                r['order'] = i
            update_preview()


def drag_release_by_image(image_name):
    global highlight_line, routine_modified
    if highlight_line:
        row = highlight_line.grid_info().get("row")
        highlight_line.destroy()
        highlight_line = None

        # non-client 루틴만 따로 분리
        non_client_routine = [r for r in routine if r['action'] != "Client"]
        dragged = next((r for r in non_client_routine if r['image'] == image_name), None)
        if not dragged:
            return

        # 재배열
        non_client_routine.remove(dragged)
        non_client_routine.insert(row, dragged)

        # order 재부여
        for i, r in enumerate(non_client_routine):
            r['order'] = i

        # 🔥 전체 routine 재조합: Client + 정렬된 나머지
        client_routine = [r for r in routine if r['action'] == "Client"]
        routine.clear()
        routine.extend(client_routine + non_client_routine)

        update_preview()
        routine_modified = True  # 루틴 변경됨


def on_closing():
    if routine_modified:
        result = messagebox.askyesnocancel("저장 확인", "루틴을 저장하지 않았습니다.\n저장하시겠습니까?", parent=root)
        if result is None:  # [취소]
            return
        elif result:        # [예]
            save_routine()
        # 아니오(False)일 경우 그냥 종료 진행
    root.destroy()


def save_action_labels():
    valid_values = {obj["value"] for obj in action_objects}
    used_labels = {
        obj["value"]: obj["label"]
        for obj in action_objects
        if obj["value"] != obj["label"]
    }

    # value-label만 저장하되, 실제 존재하는 value만 필터링
    cleaned_labels = {k: v for k, v in used_labels.items() if k in valid_values}

    with open("action_labels.json", "w", encoding="utf-8") as f:
        json.dump(cleaned_labels, f, ensure_ascii=False, indent=2)


def load_action_labels():
    if not os.path.exists("action_labels.json"):
        return {}

    try:
        with open("action_labels.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("action_labels.json 로드 실패:", e)
        return {}


# GUI 초기화 및 실행

# ✅ 최초 실행 시 삭제 버튼 상태 강제 초기화
on_action_change()

# 루틴 로드하기
load_routine()

# action 라벨 로드하기
# 사용자 정의 label 불러오기
label_map = load_action_labels()
for obj in action_objects:
    if obj["value"] in label_map:
        obj["label"] = label_map[obj["value"]]

# 바인딩
preview_canvas.bind("<MouseWheel>", on_mousewheel)
preview_frame.bind("<MouseWheel>", on_mousewheel)
conf_var.trace_add("write", sync_conf_entry)
action_var.trace_add("write", on_action_change)
conf_entry.bind("<Return>", sync_conf_slider)
conf_entry.bind("<FocusOut>", sync_conf_slider)

# 메인 실행
root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()
