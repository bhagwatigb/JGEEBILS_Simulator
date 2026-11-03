# JGEEBILS Local Exam Simulator

## Setup Instructions
1. Place your target PDF (`GS2026_QP_Biology.pdf`) inside this folder.
2. Open your terminal (e.g., your local macOS zsh terminal) and navigate to this folder.
3. Install the required dependency for reading PDF color spaces:
   `pip install pymupdf`
4. Run the parser pipeline:
   `python3 jgeebils_parser.py`

## What it does:
- Analyzes the PDF and extracts the correct answers by isolating the red-tinted text.
- Generates a `sanitized_paper.pdf` with the answer keys obscured (re-written in black).
- Automatically builds and launches a split-pane `mock_interface.html` in your default browser.
- The HTML interface features an iframe of the sanitized PDF on the left, and an active OMR panel with a 140-minute JS timer on the right.
- Applies the exact scoring rubric (+1/-0.5 for Q1-40; +2/-0.5 for Q41-60) upon submission and breaks down your performance.
