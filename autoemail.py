# 라이브러리 불러오기
import smtplib
from email.mime.text import MIMEText
import os


def ensure_file_exists(filepath, default_content=""):
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(default_content)

def init_txts():
    os.makedirs("./resources", exist_ok=True)
    ensure_file_exists("./resources/email.txt", "example@gmail.com")
    ensure_file_exists("./resources/id.txt", "example@gmail.com")
    ensure_file_exists("./resources/pw.txt", "password")
    ensure_file_exists("./resources/mail_content.txt", "Subject: 메일 제목\n---\n메일 본문 내용")

def load_email_data(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        subject_part, body_part = content.split('---', 1)
        subject = subject_part.replace('Subject:', '').strip()
        body = body_part.strip()
        return subject, body
    except ValueError:
        return "메일 제목", "메일 내용"


def send_email():
    # 개인 정보 입력(email, 앱 비밀번호)
    email_file = open("./resources/email.txt", 'r')
    id_file = open("./resources/id.txt", 'r')
    pw_file = open("./resources/pw.txt", 'r')

    # 수신할 메일 주소
    to_email = email_file.readline()

    # 발신 메일 계정 정보
    from_email = id_file.readline()
    password = pw_file.readline()

    email_file.close()
    id_file.close()
    pw_file.close()

    if to_email == "example@gmail.com" or from_email == "example@gmail.com":
        print("이메일 설정을 하지 않아 메일을 발송하지 않습니다.")
        return

    if to_email.find("@") == -1:
        print("수신할 이메일 주소가 올바르지 않아 이메일 발송이 불가합니다. email.txt파일을 확인해주세요")
        return

    check_email = from_email.find("@")
    check_domain = from_email[check_email + 1:]

    if check_email == -1:
        print("발신할 이메일 주소가 올바르지 않아 이메일 발송이 불가합니다. id.txt파일을 확인해주세요")
        return
    elif check_domain != "gmail.com" and check_domain != "naver.com":
        print(check_domain)
        print("지원되지 않는 메일 도메인입니다. 발신용 메일주소(id.txt)를 네이버 또는 GMail로 설정해주세요")
        return
    else:
        # 이메일 데이터 설정
        subject, body = load_email_data("./resources/mail_content.txt")

        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = from_email  # 발신자
        msg['To'] = to_email  # 수신자

        # smtp 서버 및 포트 설정
        smtp_server = "smtp.gmail.com"
        port = 587
        if check_domain == "naver.com":
            smtp_server = "smtp.naver.com"

        connection = smtplib.SMTP(smtp_server, port)  # 보내는 메일의 SMTP 주소입력
        connection.starttls()

        try:
            connection.login(user=from_email, password=password)
        except:
            print("발신 메일 계정 정보가 올바르지 않습니다. id.txt와 pw.txt파일을 확인해주세요.")
            return

        connection.sendmail(from_email, to_email, msg.as_string())
        connection.close()
