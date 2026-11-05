# JGEEBILS Mock Exam Simulator & Parser

A local, AI-powered exam simulator designed to parse JGEEBILS (Joint Graduate Entrance Examination for Biology and Interdisciplinary Life Sciences) PDF question papers and generate a fully interactive, timed web-based mock exam interface. 

The system uses PyMuPDF for high-resolution image extraction and a local Ollama (LLaVA) instance to digitize complex scientific text and mathematics into LaTeX.

## ✨ New Features & Updates
* **Advanced Scientific & Graphing Calculator:** A built-in, draggable calculator modal accessible during the exam and review modes. Supports standard scientific functions (sin, cos, ln, etc.) and features an interactive HTML5 canvas to plot $f(x)$ curves.
* **Instantaneous Math Rendering (KaTeX):** Replaced asynchronous MathJax with synchronous KaTeX auto-rendering. Complex binomials, fractions, and inline variables (e.g., $P(F)=0.40$, $\binom{2}{1}$) now render flawlessly and instantly without flashing raw markdown.
* **Post-Exam Analytics Filtering & Sorting:** Review your performance with an interactive toolbar. Filter questions by **Correct**, **Wrong**, or **Unattempted**, and sort them by Question Number or Marks (High to Low / Low to High).
* **Dedicated Caching System:** All generated image crops and LLaVA LaTeX outputs are now cleanly stored inside a dedicated `cache/` directory. Once a paper is processed, future loads are instantaneous, bypassing the heavy AI pipeline.

## 🚀 Prerequisites

1. **Python 3.8+**
2. **Ollama** installed locally (for the LLaVA vision model).
3. Required Python packages:
   ```bash
   pip install PyMuPDF tqdm ollama
   ```
4. Pull the LLaVA model:
   ```bash
   ollama run llava:7b
   ```

## 🛠️ Usage

1. Place your JGEEBILS PDF question papers (and any corresponding `_explanations.md` files) in a designated folder (e.g., `qps/`).
2. Run the parser script:
   ```bash
   python jgeebils_parser_v2.py --task-dir /path/to/your/qps
   ```
3. Follow the interactive CLI menu to select a paper.
4. The script will generate a `mock_interface.html` file and automatically open it in your default web browser.

## 📂 Directory Structure

```text
JGEEBILS_Simulator/
├── jgeebils_parser_v2.py        # Main parsing and HTML generation script
├── README.md                    # Project documentation
├── .gitignore                   
└── qps/                         # Your task directory
    ├── GS2026_QP_Biology.pdf
    ├── GS2026_QP_Biology_explanations.md
    ├── mock_interface.html      # Auto-generated exam UI
    └── cache/                   # Auto-generated (DO NOT COMMIT)
        ├── GS2026_QP_Biology_crops.json
        └── GS2026_QP_Biology_parsed_cache.js
```

## 📝 Exam Interface Details
* **Timer:** 140 minutes (standard JGEEBILS timing).
* **Marking Scheme:** +1 / -0.5 for Q1-Q40, and +2 / -0.5 for Q41-Q60.
* **Pause/Resume:** Ability to pause the exam (hides questions to prevent cheating).
* **Export:** Export your results to a CSV file or print the detailed analysis to a PDF.