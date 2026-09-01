import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import os

# ── Default category mapping ─────────────────────────────────────
DEFAULT_CATEGORIES = {
    "25-OH-Vitamin D": "Laboratory",
    "AFB": "Laboratory",
    "ALP": "Laboratory",
    "ASO": "Laboratory",
    "Anti-HCV Ab": "Laboratory",
    "BUN": "Laboratory",
    "Bilirubin (D)": "Laboratory",
    "Bilirubin (T)": "Laboratory",
    "Blood Film": "Laboratory",
    "Blood Group": "Laboratory",
    "CBC": "Laboratory",
    "CRP Quantitative": "Laboratory",
    "Cholesterol": "Laboratory",
    "Creatinine": "Laboratory",
    "ESR": "Laboratory",
    "Electrolyte Panel": "Laboratory",
    "FBS": "Laboratory",
    "Free T3": "Laboratory",
    "Free T4": "Laboratory",
    "H.pylori AG": "Laboratory",
    "H.pylori Ab": "Laboratory",
    "HBsAg": "Laboratory",
    "HCG": "Laboratory",
    "HDL": "Laboratory",
    "HbA1c": "Laboratory",
    "INR": "Laboratory",
    "LDH": "Laboratory",
    "LDL": "Laboratory",
    "Occult Blood": "Laboratory",
    "PSA": "Laboratory",
    "PT": "Laboratory",
    "PTT": "Laboratory",
    "RBS": "Laboratory",
    "Rheumatoid Factor (RF)": "Laboratory",
    "SGOT": "Laboratory",
    "SGPT": "Laboratory",
    "Serum Albumin": "Laboratory",
    "Stool Exam": "Laboratory",
    "TSH": "Laboratory",
    "Total Protein": "Laboratory",
    "Triglyceride (TG)": "Laboratory",
    "Troponin": "Laboratory",
    "Uric Acid": "Laboratory",
    "Urinalysis": "Laboratory",
    "VDRL": "Laboratory",
    "Weil Felix": "Laboratory",
    "Wet Film": "Laboratory",
    "Widal": "Laboratory",
    "Abdomen Ultrasound": "Ultrasound",
    "Abdominal + Pelvic Ultrasound": "Ultrasound",
    "Chest Ultrasound": "Ultrasound",
    "Foot Ultrasound Left": "Ultrasound",
    "Foot Ultrasound Right": "Ultrasound",
    "Neck Ultrasound": "Ultrasound",
    "Shoulder Ultrasound Right": "Ultrasound",
    "Thyroid Ultrasound": "Ultrasound",
    "Doppler studies of veins both legs": "Ultrasound",
    "Doppler study of arteries left leg": "Ultrasound",
    "Doppler study of veins left leg": "Ultrasound",
    "Cervical Spine X-ray": "X-ray",
    "Chest AP/PA and lateral/oblique": "X-ray",
    "Chest X-ray AP and PA": "X-ray",
    "Chest for Ribs X-ray": "X-ray",
    "Femoral Head X-ray Right": "X-ray",
    "Hand X-ray Right": "X-ray",
    "Knee joint left X-ray": "X-ray",
    "Knee joint right X-ray": "X-ray",
    "Paranasal Sinuses X-ray": "X-ray",
    "Pelvic X-ray": "X-ray",
    "Pelvic bone (hip joint) right x-ray": "X-ray",
    "Shoulder X-ray Right": "X-ray",
    "ECG": "ECG",
    "Echocardiography": "Echocardiography",
    "IM injection": "Nursing & Procedures",
    "IV injection": "Nursing & Procedures",
    "Observation": "Nursing & Procedures",
    "Oxygen per hour": "Nursing & Procedures",
    "Buttock Left": "Nursing & Procedures",
    "Buttock Right": "Nursing & Procedures",
    "IP - Disposable Glove pair": "Supplies",
    "IP - Dressing Medium": "Supplies",
    "Consultancy of Internist Card": "Consultation",
}

# ── Desired column order in output ────────────────────────────────
COLUMN_ORDER = [
    "Consultation",
    "Laboratory",
    "X-ray",
    "Ultrasound",
    "ECG",
    "Echocardiography",
    "Nursing & Procedures",
    "Supplies",
]


def reorder_columns(pivot):
    """Reorder pivot columns, keeping TOTAL at the far right."""
    ordered = [c for c in COLUMN_ORDER if c in pivot.columns]
    extras = [c for c in pivot.columns if c not in COLUMN_ORDER and c != "TOTAL"]
    total = ["TOTAL"] if "TOTAL" in pivot.columns else []
    return pivot[ordered + extras + total]


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Service Analyzer")
        self.root.geometry("900x620")

        self.df = None
        self.file_path = None
        self.category_vars = {}  # service_name -> StringVar

        # ── Top bar: file selection ──────────────────────────────
        top = tk.Frame(root)
        top.pack(fill=tk.X, padx=5, pady=5)

        tk.Button(top, text="Open Excel File", command=self.open_file).pack(side=tk.LEFT)
        self.file_label = tk.Label(top, text="No file loaded", anchor="w")
        self.file_label.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        # ── Middle: service-category table ───────────────────────
        mid = tk.LabelFrame(root, text="Service -> Category Mapping")
        mid.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Scrollable canvas
        canvas = tk.Canvas(mid)
        scrollbar = tk.Scrollbar(mid, orient=tk.VERTICAL, command=canvas.yview)
        self.table_frame = tk.Frame(canvas)

        self.table_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.table_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Enable mouse-wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.canvas = canvas

        # ── Bottom bar: generate button ──────────────────────────
        bot = tk.Frame(root)
        bot.pack(fill=tk.X, padx=5, pady=5)

        tk.Button(bot, text="Generate Summary", command=self.generate).pack(side=tk.LEFT)
        self.status_label = tk.Label(bot, text="", anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

    # ── Load file ────────────────────────────────────────────────
    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")]
        )
        if not path:
            return

        try:
            self.df = pd.read_excel(path)
            self.file_path = path
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read file:\n{e}")
            return

        # Verify required columns
        required = {"Patient Name", "Service", "Amount", "SoT Date"}
        found = set(self.df.columns)
        missing = required - found
        if missing:
            messagebox.showerror(
                "Missing Columns",
                f"The file is missing columns: {', '.join(missing)}\n\nFound: {', '.join(found)}",
            )
            self.df = None
            return

        self.file_label.config(text=os.path.basename(path))
        self.populate_table()

    # ── Populate mapping table ───────────────────────────────────
    def populate_table(self):
        # Clear old widgets
        for w in self.table_frame.winfo_children():
            w.destroy()
        self.category_vars.clear()

        # Collect all known category names for the dropdown
        all_categories = sorted(set(DEFAULT_CATEGORIES.values()) | {"Other"})

        # Header
        tk.Label(self.table_frame, text="#", width=4, relief=tk.RIDGE).grid(row=0, column=0, sticky="ew")
        tk.Label(self.table_frame, text="Service", width=40, anchor="w", relief=tk.RIDGE).grid(row=0, column=1, sticky="ew")
        tk.Label(self.table_frame, text="Category", width=25, relief=tk.RIDGE).grid(row=0, column=2, sticky="ew")

        services = sorted(self.df["Service"].dropna().unique())

        for i, svc in enumerate(services, start=1):
            default_cat = DEFAULT_CATEGORIES.get(svc, "Other")
            var = tk.StringVar(value=default_cat)
            self.category_vars[svc] = var

            # If category not in list, add it
            cats = all_categories if default_cat in all_categories else all_categories + [default_cat]

            tk.Label(self.table_frame, text=str(i), width=4).grid(row=i, column=0)
            tk.Label(self.table_frame, text=svc, anchor="w", width=40).grid(row=i, column=1, sticky="w")
            combo = ttk.Combobox(self.table_frame, textvariable=var, values=cats, width=22)
            combo.grid(row=i, column=2, padx=2, pady=1)

        self.status_label.config(text=f"Loaded {len(services)} unique services.")

    # ── Generate summary ─────────────────────────────────────────
    def generate(self):
        if self.df is None:
            messagebox.showwarning("No data", "Please open an Excel file first.")
            return

        # Build category map from current UI state
        cat_map = {svc: var.get() for svc, var in self.category_vars.items()}

        df = self.df.copy()
        df["Category"] = df["Service"].map(cat_map).fillna("Other")
        df["SoT Date"] = pd.to_datetime(df["SoT Date"], errors="coerce").dt.date

        # Ask where to save
        save_path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx")],
            initialfile="summary_by_date.xlsx",
            initialdir=os.path.dirname(self.file_path),
        )
        if not save_path:
            return

        try:
            dates = sorted(df["SoT Date"].dropna().unique())

            with pd.ExcelWriter(save_path, engine="openpyxl") as writer:
                for dt in dates:
                    day_df = df[df["SoT Date"] == dt]
                    pivot = day_df.pivot_table(
                        index="Patient Name",
                        columns="Category",
                        values="Amount",
                        aggfunc="sum",
                        fill_value=0,
                        margins=True,
                        margins_name="TOTAL",
                    )
                    if "TOTAL" in pivot.index:
                        total_row = pivot.loc[["TOTAL"]]
                        rest = pivot.drop("TOTAL").sort_values("TOTAL", ascending=False)
                        pivot = pd.concat([rest, total_row])

                    pivot = reorder_columns(pivot)
                    pivot.to_excel(writer, sheet_name=str(dt))

                # Grand summary
                grand = df.pivot_table(
                    index="Patient Name",
                    columns="Category",
                    values="Amount",
                    aggfunc="sum",
                    fill_value=0,
                    margins=True,
                    margins_name="TOTAL",
                )
                if "TOTAL" in grand.index:
                    total_row = grand.loc[["TOTAL"]]
                    rest = grand.drop("TOTAL").sort_values("TOTAL", ascending=False)
                    grand = pd.concat([rest, total_row])
                grand = reorder_columns(grand)
                grand.to_excel(writer, sheet_name="Grand Summary")

                # Raw data
                df.to_excel(writer, sheet_name="Raw Data", index=False)

            self.status_label.config(text=f"Saved to {os.path.basename(save_path)}")
            messagebox.showinfo("Done", f"Summary saved to:\n{save_path}")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to generate summary:\n{e}")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
