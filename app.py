"""Local file picker for the verified PDF signer; no web service."""
import os
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from signer import process

ROOT = Path(__file__).resolve().parent

class App:
    def __init__(self, window):
        self.window = window
        self.events = queue.Queue()
        self.busy = False
        self.result = None
        window.title('PDF Signer / Заявки ЖД')
        window.geometry('660x420')
        window.minsize(600, 380)
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Подписать заявку ЖД', font=('Segoe UI', 18)).pack(anchor='w')
        ttk.Label(frame, text='Выберите PDF без подписи и печати.\nГотовый файл будет сохранён отдельно.', wraplength=590).pack(anchor='w', pady=(12,18))
        self.choose = ttk.Button(frame, text='Выбрать PDF и подписать', command=self.select)
        self.choose.pack(anchor='w')
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x', pady=(18,12))
        self.message = tk.StringVar(value='Ожидается PDF. Подпись разрешена только для подтверждённого перевозчика.')
        ttk.Label(frame, textvariable=self.message, wraplength=590, justify='left').pack(anchor='w')
        self.details = tk.Text(frame, height=6, wrap='word', relief='flat', state='disabled', font=('Segoe UI',9))
        self.details.pack(fill='both', expand=True, pady=12)
        actions = ttk.Frame(frame)
        actions.pack(anchor='w')
        self.open_pdf = ttk.Button(actions, text='Открыть готовый PDF', command=self.open_result, state='disabled')
        self.open_pdf.pack(side='left')
        self.open_folder = ttk.Button(actions, text='Открыть папку результата', command=self.open_directory, state='disabled')
        self.open_folder.pack(side='left', padx=12)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(150, self.poll)

    def set_details(self, text):
        self.details.configure(state='normal')
        self.details.delete('1.0','end')
        self.details.insert('1.0',text)
        self.details.configure(state='disabled')

    def select(self):
        selected = filedialog.askopenfilename(parent=self.window, title='Выберите PDF заявки', filetypes=[('PDF','*.pdf')])
        if not selected:
            return
        self.busy = True
        self.result = None
        self.choose.configure(state='disabled')
        self.open_pdf.configure(state='disabled')
        self.open_folder.configure(state='disabled')
        self.progress.start(12)
        self.message.set('Проверяю страницы и создаю подписанный PDF…')
        self.set_details(Path(selected).name)
        threading.Thread(target=self.work, args=(Path(selected),), daemon=True).start()

    def work(self, source):
        folder = ROOT/'output'/'manual'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        try:
            result, rows = process(source, ROOT/'config.local.json', folder)
            self.events.put(('done', result, rows))
        except Exception:
            self.events.put(('error', None, None))

    def poll(self):
        try:
            kind, result, rows = self.events.get_nowait()
        except queue.Empty:
            self.window.after(150,self.poll)
            return
        self.busy = False
        self.progress.stop()
        self.choose.configure(state='normal')
        if kind == 'error':
            self.message.set('Не удалось обработать PDF. Исходный файл не заменён.')
            self.set_details('Проверьте, что PDF открывается без пароля и локальное факсимиле доступно. Если ошибка повторяется, передайте файл на ручную проверку.')
        else:
            self.result = result
            count = {s:sum(r['status']==s for r in rows) for s in ['SIGNED','ALREADY_SIGNED','REVIEW_REQUIRED','ERROR']}
            if count['REVIEW_REQUIRED'] or count['ERROR']:
                self.message.set(f"Файл создан. Подписано: {count['SIGNED']}. Уже подписано: {count['ALREADY_SIGNED']}. Требуют проверки: {count['REVIEW_REQUIRED']}. Ошибок: {count['ERROR']}.")
            else:
                self.message.set(f"Готово. Подписано страниц: {count['SIGNED']}. Уже подписано: {count['ALREADY_SIGNED']}.")
            labels = dict(SIGNED='подписана',ALREADY_SIGNED='уже подписана',REVIEW_REQUIRED='не подписана — требуется проверка',ERROR='не подписана — ошибка обработки')
            text = '\n'.join(f"Страница {r['page']}: {labels[r['status']]}" for r in rows)
            text += '\n\nРезультат: '+str(result)
            if count['REVIEW_REQUIRED'] or count['ERROR']:
                text += '\nПричины по каждой странице сохранены в журнале рядом с PDF. Проверьте неподписанные страницы перед отправкой.'
            self.set_details(text)
            self.open_pdf.configure(state='normal')
            self.open_folder.configure(state='normal')
        self.window.after(150,self.poll)

    def open_result(self):
        if self.result:
            os.startfile(str(self.result))

    def open_directory(self):
        if self.result:
            os.startfile(str(self.result.parent))

    def close(self):
        if self.busy:
            self.message.set('Дождитесь завершения обработки, затем закройте окно.')
        else:
            self.window.destroy()

if __name__ == '__main__':
    window = tk.Tk()
    App(window)
    window.mainloop()
