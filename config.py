# 전체 코드 재작성: 기능 통합 및 최적화

import tkinter as tk
from tkinter import messagebox, Scrollbar, Canvas
from PIL import Image, ImageTk
import os
import json
import path_manager as pm


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

image_folder = pm.get_img_path()

root = tk.Tk()
root.title("루틴 설정")
root.geometry("900x700")
root.resizable(False, False)

selected_image_var = tk.StringVar()
action_var = tk.StringVar(value="click")
action_options = ["click", "space", "s", "esc", "Client"]
action_var.trace_add("write", lambda *args: action_display_label.config(text=f"선택된 동작: {action_var.get()}"))

selected_routine_index = None

# ---------- GUI ----------
# 이미지 목록
top_frame = tk.Frame(root)
top_frame.pack(pady=10, fill="x")
tk.Label(top_frame, text="🖼 이미지 파일 목록", font=("맑은 고딕", 13, "bold")).pack()

canvas = Canvas(top_frame, height=220, width=820)
scroll_y = Scrollbar(top_frame, orient="vertical", command=canvas.yview)
frame_thumbs = tk.Frame(canvas)
frame_thumbs.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
canvas.create_window((410, 0), window=frame_thumbs, anchor='n')
canvas.configure(yscrollcommand=scroll_y.set)
canvas.pack(side="top", fill="both", expand=True)
scroll_y.pack(side="right", fill="y")

row, col = 0, 0
for file_name in os.listdir(image_folder):
    if file_name.lower().endswith(".png"):
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

# 본문
main_frame = tk.Frame(root)
main_frame.pack(padx=20, fill="both", expand=True)

left_frame = tk.Frame(main_frame)
left_frame.pack(side="left", anchor="n", padx=(0, 40))

# 선택 정보
tk.Label(left_frame, text="선택된 이미지:", font=("맑은 고딕", 11)).pack(anchor="w")
selected_image_thumb = tk.Label(left_frame)
selected_image_thumb.pack(anchor="w", pady=(0, 5))
selected_label = tk.Label(left_frame, text="없음", font=("맑은 고딕", 11, "bold"), anchor="w", width=40)
selected_label.pack(anchor="w")

# 동작
tk.Label(left_frame, text="입력 동작 선택", font=("맑은 고딕", 11)).pack(anchor="w", pady=(10, 0))
action_menu = tk.OptionMenu(left_frame, action_var, *action_options)
action_menu.pack(anchor="w")
action_display_label = tk.Label(left_frame, text="선택된 동작: click", font=("맑은 고딕", 10), anchor="w")
action_display_label.pack(anchor="w", pady=(0, 10))


# 우측 루틴 미리보기
right_frame = tk.Frame(main_frame)
right_frame.pack(side="left", anchor="n")
tk.Label(right_frame, text="📋 현재 루틴 미리보기", font=("맑은 고딕", 12, "bold")).pack(anchor="w")
tk.Label(right_frame, text="※ 항목을 우클릭하면 삭제됩니다", font=("맑은 고딕", 9), fg="gray").pack(anchor="w", pady=(0, 0))
tk.Label(right_frame, text="※ 드래그하여 순서를 변경할 수 있습니다", font=("맑은 고딕", 9), fg="gray").pack(anchor="w", pady=(0, 5))
preview_canvas_wrapper = tk.Frame(right_frame, height=200)
preview_canvas_wrapper.pack(anchor="w")
preview_scroll = Scrollbar(preview_canvas_wrapper, orient="vertical")
preview_scroll.pack(side="right", fill="y")
preview_canvas = Canvas(preview_canvas_wrapper, yscrollcommand=preview_scroll.set, height=200, width=420)
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
                continue
            img = Image.open(path)
            img.thumbnail((50, 50))
            tk_img = ImageTk.PhotoImage(img)
            img_label = tk.Label(client_display, image=tk_img)
            img_label.image = tk_img
            img_label.grid(row=0, column=0, padx=5, pady=5)
            text_label = tk.Label(client_display, text=item['image'], font=("맑은 고딕", 10), anchor="w")
            text_label.grid(row=0, column=1, sticky="w")
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
        lbl.bind("<Button-1>", lambda e, i=idx: select_from_routine(i))
        lbl.bind("<B1-Motion>", lambda e: drag_motion(e))
        lbl.bind("<ButtonRelease-1>", lambda e, i=idx: drag_release(i))
        lbl.bind("<Button-3>", lambda e, i=idx: delete_routine(i))

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

    match = next((r for r in routine if r['image'] == name), None)
    if match:
        action_var.set(match['action'])
    else:
        action_var.set("click")

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
    menu = action_menu['menu']
    for i, label in enumerate(action_options):
        if label == "Client":
            if any(r['action'] == "Client" and r['image'] != item['image'] for r in routine):
                menu.entryconfig(i, state="disabled")
            else:
                menu.entryconfig(i, state="normal")
    item = routine[index]
    select_image(item['image'])

def update_client_action_menu():
    menu = action_menu['menu']
    if any(r['action'] == "Client" for r in routine):
        for i, label in enumerate(action_options):
            if label == "Client":
                menu.entryconfig(i, state="disabled")
    else:
        for i, label in enumerate(action_options):
            menu.entryconfig(i, state="normal")

def update_save_button_state():
    if routine:
        save_btn.config(state="normal")
    else:
        save_btn.config(state="disabled")

def add_routine():
    global selected_image
    if not selected_image:
        return

    action = action_var.get()
    existing = next((r for r in routine if r['image'] == selected_image), None)

    if action == "Client":
        # 이미 다른 이미지가 Client로 설정되어 있으면 추가 불가
        if any(r['action'] == "Client" and r['image'] != selected_image for r in routine):
            messagebox.showwarning("중복 불가", "이미 Client로 설정된 항목이 존재합니다.")
            return

    if existing:
        if existing['action'] != "Client" and action == "Client":
            # 일반 → Client : 기존 항목 제거, 순서 재정렬, Client로 재추가
            routine.remove(existing)
            non_client = [r for r in routine if r['action'] != "Client"]
            for i, r in enumerate(non_client):
                r['order'] = i
            routine.append({"image": selected_image, "action": "Client"})
            messagebox.showinfo("변경됨", f"{selected_image}가 Client 항목으로 변경되었습니다.")
        elif existing['action'] == "Client" and action != "Client":
            # Client → 일반 : order 부여 후 상태 변경
            existing['action'] = action
            existing['order'] = len([r for r in routine if r['action'] != "Client"])
            messagebox.showinfo("변경됨", f"{selected_image}가 일반 루틴 항목으로 변경되었습니다.")
        else:
            # 일반 항목의 동작만 수정
            existing['action'] = action
            messagebox.showinfo("수정됨", f"{selected_image}의 동작이 수정되었습니다.")
    else:
        # 새 항목 추가
        if action == "Client":
            routine.append({"image": selected_image, "action": "Client"})
        else:
            routine.append({"image": selected_image, "action": action, "order": len([r for r in routine if r['action'] != "Client"])})
        image_buttons[selected_image].config(state="disabled")
        messagebox.showinfo("추가됨", f"{selected_image}가 루틴에 추가되었습니다.")

    update_preview()
    update_client_action_menu()  # ← Client 항목 유무에 따라 메뉴 상태 갱신

    # UI 초기화
    add_btn.config(state="disabled")
    selected_image_var.set("")
    selected_label.config(text="없음")
    selected_image_thumb.config(image="")
    selected_image_thumb.image = None
    update_save_button_state()

def delete_routine(index):
    image = routine[index]['image']
    routine.pop(index)
    for i, r in enumerate(routine):
        r['order'] = i
    if image in image_buttons:
        image_buttons[image].config(state="normal")

    update_client_action_menu()

    update_preview()
    update_save_button_state()

def save_routine():
    with open(ROUTINE_FILE, "w", encoding="utf-8") as f:
        json.dump(routine, f, indent=2)
    messagebox.showinfo("저장 완료", "routine.json 파일로 저장되었습니다!")

def load_routine():
    global routine
    if os.path.exists(ROUTINE_FILE):
        try:
            with open(ROUTINE_FILE, "r", encoding="utf-8") as f:
                routine = json.load(f)

            # 이미지 목록 버튼 중 루틴에 포함된 이미지 비활성화
            for item in routine:
                if item['image'] in image_buttons:
                    image_buttons[item['image']].config(state="disabled")

            update_preview()
            update_client_action_menu()
        except Exception as e:
            messagebox.showerror("로드 실패", f"routine.json 파일을 불러오는 중 오류 발생:\n{e}")

    update_save_button_state()

# 버튼 프레임
btn_frame = tk.Frame(left_frame)
btn_frame.pack(anchor="w")

#추가/수정 버튼
add_btn = tk.Button(btn_frame, text="추가 / 수정", command=add_routine, state="disabled")
add_btn.grid(row=0, column=0, padx=5)

#루틴저장 버튼
save_btn = tk.Button(btn_frame, text="루틴 저장", command=save_routine)
save_btn.grid(row=0, column=1, padx=5)
save_btn.config(state="disabled")  # 처음엔 비활성화


def drag_motion(event):
    global highlight_line
    labels = [w for w in preview_frame.winfo_children() if isinstance(w, tk.Label)]
    mouse_y = event.y_root
    target_row = None

    for i, label in enumerate(labels):
        top = label.winfo_rooty()
        bottom = top + label.winfo_height()
        if top <= mouse_y <= bottom:
            target_row = i // 2  # 이미지/텍스트 라벨 번갈아 있으므로 2로 나눔
            break

    # 🔽 자동 스크롤 조건 처리
    canvas_top = preview_canvas.winfo_rooty()
    canvas_bottom = canvas_top + preview_canvas.winfo_height()

    if mouse_y < canvas_top + 20:
        preview_canvas.yview_scroll(-1, "units")  # 위로 스크롤
    elif mouse_y > canvas_bottom - 20:
        preview_canvas.yview_scroll(1, "units")  # 아래로 스크롤

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


#GUI 초기화 및 실행
load_routine()
update_preview()
preview_canvas.bind("<MouseWheel>", on_mousewheel)
preview_frame.bind("<MouseWheel>", on_mousewheel)
root.mainloop()
