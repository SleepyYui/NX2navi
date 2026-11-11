"""The Game Patcher window, launched from the launcher or Game Patcher.exe."""
import argparse
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
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
        ('out', ''))}
    status = tk.StringVar(value='Select the full ZIP you previously copied and the updated game folder.')
    events = queue.Queue()
    cancelled = threading.Event()
    active = False
    closing = False

    def choose(key):
        if key == 'game':
            path = filedialog.askdirectory(parent=root, title='Updated game folder', initialdir=values[key].get())
        elif key == 'base':
            path = filedialog.askopenfilename(parent=root, title='Original full game ZIP', filetypes=[('ZIP archives', '*.zip')])
        else:
            path = filedialog.asksaveasfilename(parent=root, title='Save changed files as a ZIP',
                                              defaultextension='.zip', filetypes=[('ZIP archives', '*.zip')],
                                              initialfile='Ocarina of Time - Patch.zip')
        if path:
            values[key].set(path)
            if key == 'base' and not values['out'].get():
                values['out'].set(str(Path(path).with_name(Path(path).stem + ' - Patch.zip')))

    for row, (key, label) in enumerate((('base', 'Original ZIP'), ('game', 'Updated game folder'), ('out', 'Patch ZIP'))):
        ttk.Label(pane, text=label).grid(row=row, column=0, padx=(0, 12), pady=5, sticky='w')
        ttk.Entry(pane, textvariable=values[key], width=58).grid(row=row, column=1, sticky='ew', pady=5)
        ttk.Button(pane, text='Browse', command=lambda key=key: choose(key)).grid(row=row, column=2, padx=(8, 0))
    bar = ttk.Progressbar(pane, maximum=1)
    bar.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(15, 8))
    ttk.Label(pane, textvariable=status, wraplength=640).grid(row=4, column=0, columnspan=3, sticky='w')

    def start():
        nonlocal active
        if any(not value.get().strip() for value in values.values()):
            messagebox.showerror('Game Patcher', 'Select the original ZIP, updated game folder and output ZIP.', parent=root)
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
    create_button.grid(row=5, column=1, sticky='e', pady=(16, 0))
    cancel_button = ttk.Button(pane, text='Cancel', state='disabled', command=cancelled.set)
    cancel_button.grid(row=5, column=2, padx=(8, 0), pady=(16, 0))
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(100, poll)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
