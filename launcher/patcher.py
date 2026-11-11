"""The Game Patcher window, launched from the launcher or Game Patcher.exe."""
import argparse
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, ttk

if __name__ == '__main__' and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'launcher'

from . import game_patch, net
from .context import Context
from .paths import Layout


def main(argv=None):
    parser = argparse.ArgumentParser(prog='Game Patcher')
    parser.add_argument('--root')
    args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    ctx = Context(Layout(root=args.root) if args.root else None)
    root = tk.Tk()
    root.title('Ocarina of Time Game Patcher')
    root.minsize(680, 260)
    pane = ttk.Frame(root, padding=18)
    pane.grid(sticky='nsew')
    root.columnconfigure(0, weight=1)
    pane.columnconfigure(1, weight=1)
    values = {key: tk.StringVar(value=value) for key, value in (
        ('base', ctx.settings.get('patch_base_zip', '')),
        ('game', ctx.settings.get('game_dir', '')),
        ('out', ''), ('from', ''), ('to', ''))}
    status = tk.StringVar(value='Select the full ZIP you previously copied and the updated game folder.')
    events = queue.Queue()
    cancelled = threading.Event()
    active = False
    closing = False
    detected_paths = None
    suggested_output = ''

    def suggest_output(*_):
        nonlocal suggested_output
        try:
            name = game_patch.patch_filename(values['from'].get(), values['to'].get())
        except ValueError:
            return
        if values['base'].get():
            path = str(Path(values['base'].get()).with_name(name))
            if not values['out'].get() or values['out'].get() == suggested_output:
                values['out'].set(path)
            suggested_output = path

    def detect_versions(*_):
        nonlocal detected_paths
        paths = values['game'].get(), values['base'].get()
        if active or not all(paths) or paths == detected_paths:
            return
        detected_paths = paths
        values['from'].set('')
        values['to'].set('')
        status.set('Reading game versions...')

        def detect():
            try:
                result = game_patch.patch_versions(*paths)
            except (OSError, ValueError, zipfile.BadZipFile):
                result = ('', '')
            events.put(('versions', (paths, result)))

        threading.Thread(target=detect, daemon=True).start()

    for key in ('from', 'to'):
        values[key].trace_add('write', suggest_output)

    def choose(key):
        if key == 'game':
            path = filedialog.askdirectory(parent=root, title='Updated game folder', initialdir=values[key].get())
        elif key == 'base':
            path = filedialog.askopenfilename(parent=root, title='Original full game ZIP', filetypes=[('ZIP archives', '*.zip')])
        else:
            path = filedialog.asksaveasfilename(parent=root, title='Save changed files as a ZIP',
                                              defaultextension='.zip', filetypes=[('ZIP archives', '*.zip')],
                                              initialfile=Path(values['out'].get()).name or 'Ocarina of Time - Patch.zip')
        if path:
            values[key].set(path)
            if key != 'out':
                detect_versions()

    for row, (key, label) in enumerate((('base', 'Original ZIP'), ('game', 'Updated game folder'), ('out', 'Patch ZIP'))):
        ttk.Label(pane, text=label).grid(row=row, column=0, padx=(0, 12), pady=5, sticky='w')
        entry = ttk.Entry(pane, textvariable=values[key], width=58)
        entry.grid(row=row, column=1, sticky='ew', pady=5)
        if key != 'out':
            entry.bind('<FocusOut>', detect_versions)
        ttk.Button(pane, text='Browse', command=lambda key=key: choose(key)).grid(row=row, column=2, padx=(8, 0))
    for row, (key, label) in enumerate((('from', 'From version'), ('to', 'To version')), start=3):
        ttk.Label(pane, text=label).grid(row=row, column=0, sticky='w', pady=5)
        ttk.Entry(pane, textvariable=values[key]).grid(row=row, column=1, sticky='ew', pady=5)
    bar = ttk.Progressbar(pane, maximum=1)
    bar.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(15, 8))
    ttk.Label(pane, textvariable=status, wraplength=640).grid(row=6, column=0, columnspan=3, sticky='w')

    def start():
        nonlocal active
        if any(not value.get().strip() for value in values.values()):
            messagebox.showerror('Game Patcher', 'Select the ZIPs and game folder. Enter both versions if detection is unavailable.', parent=root)
            return
        try:
            game_patch.patch_filename(values['from'].get(), values['to'].get())
        except ValueError as error:
            messagebox.showerror('Game Patcher', str(error), parent=root)
            return
        inputs = {key: value.get() for key, value in values.items()}
        active = True
        cancelled.clear()
        create_button.configure(state='disabled')
        cancel_button.configure(state='normal')
        status.set('Comparing game files. The original ZIP stays available for future patches.')

        def work():
            try:
                result = game_patch.create(inputs['game'], inputs['base'], inputs['out'], cancel=cancelled.is_set,
                                           from_version=inputs['from'], to_version=inputs['to'],
                                           progress=lambda fraction, detail: events.put(('progress', (fraction, detail))))
                events.put(('done', result))
            except net.Cancelled:
                events.put(('cancelled', None))
            except Exception as error:
                ctx.log(f'game patch: {error}')
                events.put(('error', str(error)))

        threading.Thread(target=work, daemon=True).start()

    def close():
        nonlocal closing
        if active:
            closing = True
            cancelled.set()
        else:
            root.destroy()

    def poll():
        nonlocal active
        for _ in range(200):
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == 'versions':
                paths, detected = value
                if paths == (values['game'].get(), values['base'].get()) and not active:
                    for key, version in zip(('from', 'to'), detected):
                        if not values[key].get():
                            values[key].set(version)
                    status.set('Check the versions, then create the patch.' if all(detected) else
                               'A version could not be detected. Enter the missing version above.')
                continue
            if kind == 'progress':
                bar['value'], text = value
                status.set(text)
                continue
            active = False
            create_button.configure(state='normal')
            cancel_button.configure(state='disabled')
            if kind == 'done':
                ctx.settings['patch_base_zip'] = str(value['baseline'])
                ctx.settings.save()
                status.set(f'Created {value["changed"]} changed files in {value["size"] / 1e6:.1f} MB. '
                           + ('Read PATCH-README.txt for obsolete files to remove.' if value['removed'] else
                              'Extract the patch over your older game folder.'))
            elif kind == 'cancelled':
                status.set('Cancelled.')
            else:
                status.set(value)
                if not closing:
                    messagebox.showerror('Game Patcher', value, parent=root)
        if closing and not active:
            root.destroy()
            return
        root.after(100, poll)

    create_button = ttk.Button(pane, text='Create Patch', command=start)
    create_button.grid(row=7, column=1, sticky='e', pady=(16, 0))
    cancel_button = ttk.Button(pane, text='Cancel', state='disabled', command=cancelled.set)
    cancel_button.grid(row=7, column=2, padx=(8, 0), pady=(16, 0))
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(100, poll)
    root.after(150, detect_versions)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
