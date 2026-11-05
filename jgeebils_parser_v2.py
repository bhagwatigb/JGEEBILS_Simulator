import fitz  # PyMuPDF
import json
import os
import webbrowser
import argparse
import glob
import re
import base64
import sys

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False


def format_markdown_math(text):
    """
    Converts markdown bold/italics and paragraphs while keeping LaTeX expressions untouched.
    """
    # Stash display math ($$...$$) and inline math ($...$)
    math_tokens = {}
    counter = [0]

    def stash_display(m):
        tok = f"___DISPMATH{counter[0]}___"
        math_tokens[tok] = m.group(0)
        counter[0] += 1
        return tok

    def stash_inline(m):
        tok = f"___INLINEMATH{counter[0]}___"
        math_tokens[tok] = m.group(0)
        counter[0] += 1
        return tok

    # Extract math blocks safely
    text = re.sub(r'\$\$(.*?)\$\$', stash_display, text, flags=re.DOTALL)
    text = re.sub(r'(?<!\\)\$([^\$\n]+?)\$', stash_inline, text)

    # Markdown bold and italics
    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', text)

    # Paragraph formatting
    blocks = [b.strip().replace('\n', ' ') for b in text.split('\n\n') if b.strip()]
    text = "".join(f"<p>{b}</p>" for b in blocks)

    # Restore math blocks
    for tok, orig in math_tokens.items():
        text = text.replace(tok, orig)

    return text


def crop_questions_from_pdf(doc):
    """
    Detects question boundaries on each page and crops crisp 180 DPI images
    preserving diagrams, flowcharts, chemical structures, and fractions.
    """
    print("  -> Cropping high-resolution question images from PDF...")
    question_crops = {}
    expected_q = 1
    page_questions = []

    for page_idx, page in enumerate(doc):
        blocks = page.get_text("dict").get("blocks", [])
        for b in blocks:
            if b.get("type") == 0:
                for line in b.get("lines", []):
                    line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    bbox = line.get("bbox")
                    if not bbox:
                        continue
                    x0, y0, x1, y1 = bbox

                    if x0 < 160:
                        m = re.match(r'^(?:Question|Q\.?)?\s*(\d+)[\.\)]\s*', line_text, re.IGNORECASE)
                        if m:
                            num = int(m.group(1))
                            section_q = ((expected_q - 1) % 15) + 1
                            if num == expected_q or num == section_q:
                                page_questions.append((page_idx, expected_q, y0))
                                expected_q += 1
                                if expected_q > 60:
                                    break
            if expected_q > 60:
                break

    for idx, (p_idx, q_num, y_start) in enumerate(page_questions):
        page = doc[p_idx]
        p_height = page.rect.height
        p_width = page.rect.width

        if idx + 1 < len(page_questions) and page_questions[idx + 1][0] == p_idx:
            y_end = page_questions[idx + 1][2] - 4
        else:
            y_end = p_height - 35

        y_top = max(y_start - 6, 35)

        clip_rect = fitz.Rect(0, y_top, p_width, min(y_end, p_height))
        pix = page.get_pixmap(clip=clip_rect, dpi=180)
        img_b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")

        if q_num not in question_crops:
            question_crops[q_num] = []
        question_crops[q_num].append(f"data:image/png;base64,{img_b64}")

        if idx + 1 < len(page_questions):
            next_p_idx, next_q, next_y = page_questions[idx + 1]
            if next_p_idx > p_idx and next_y > 100:
                cont_page = doc[next_p_idx]
                cont_rect = fitz.Rect(0, 35, cont_page.rect.width, min(next_y - 4, cont_page.rect.height))
                cont_pix = cont_page.get_pixmap(clip=cont_rect, dpi=180)
                cont_b64 = base64.b64encode(cont_pix.tobytes("png")).decode("utf-8")
                question_crops[q_num].append(f"data:image/png;base64,{cont_b64}")

    return question_crops


def parse_pdf_with_ollama(doc):
    if not OLLAMA_AVAILABLE:
        return {}, {}

    print("  -> Processing pages with Ollama (llava:7b) in LaTeX mode...")
    answer_key = {}
    latex_questions = {}

    prompt = """
    You are an expert STEM document digitizer. Read this exam paper page:
    1. Identify every question number on the page.
    2. Extract the question statement and all four choices.
    3. Format ALL mathematical expressions, fractions, chemical formulas, and scientific notations strictly in LaTeX using inline dollar signs (e.g. $Zz$, $\\frac{1}{2}$, $\\text{Ca}^{2+}$, $\\mu\\text{m}$, $10^{-4}$).
    4. Identify which option (a, b, c, or d) is marked in RED text as the correct answer.
    5. Return STRICTLY a valid JSON array. Do not include markdown code fences or conversational text.

    Format:
    [
      {
        "q_num": 1,
        "question_text": "What is the concentration of $\\text{H}^+$?<br>(a) $10^{-7}\\text{ M}$<br>(b) $10^{-3}\\text{ M}$<br>(c) $10^{-14}\\text{ M}$<br>(d) None",
        "marked_answer": "b"
      }
    ]
    """

    for i, page in enumerate(tqdm(doc, desc="     LLaVA Page Scan", unit="page", ncols=80)):
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")

        try:
            response = ollama.chat(
                model='llava:7b',
                messages=[{
                    'role': 'user',
                    'content': prompt,
                    'images': [img_bytes]
                }],
                format='json',
                options={'temperature': 0.0}
            )

            raw_content = response['message']['content'].strip()
            if raw_content.startswith("```json"):
                raw_content = raw_content[7:-3].strip()

            page_data = json.loads(raw_content)
            
            # Robustly handle LLaVA hallucinating a dict or string instead of a list
            if isinstance(page_data, dict):
                page_data = [page_data]
            elif not isinstance(page_data, list):
                page_data = []
                
            for item in page_data:
                if not isinstance(item, dict):
                    continue
                q_num = int(item.get("q_num", 0))
                if q_num == 0:
                    continue
                ans = str(item.get("marked_answer", "")).lower().strip()
                q_text = item.get("question_text", "")

                if ans in ['a', 'b', 'c', 'd']:
                    answer_key[q_num] = ans
                if q_text:
                    latex_questions[q_num] = q_text

        except Exception as e:
            print(f"     [!] Ollama scan skipped on page {i + 1}: {e}")

    return answer_key, latex_questions


def parse_explanations(md_path):
    print(f"Parsing explanations from {os.path.basename(md_path)}...")
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    pattern = r"### Question\s+(\d+)(.*?)\*\*Explanation:\*\*\s*(.*?)(?=\n---|\n### Question|\Z)"
    matches = re.finditer(pattern, content, re.DOTALL | re.IGNORECASE)

    explanations = {}
    verified_answers = {}
    detected_exp_q = 1

    for m in matches:
        header_text = m.group(2).strip()
        raw_exp_text = m.group(3).strip()

        # Safely format explanation text without mangling LaTeX blocks
        exp_text = format_markdown_math(raw_exp_text)

        ans_match = re.search(r'\*\*Correct Answer:\*\*\s*([A-Za-z0-9/\*]+)', header_text, re.IGNORECASE)
        if ans_match:
            raw_ans = ans_match.group(1).strip().lower()
            if raw_ans in ['a', 'b', 'c', 'd'] or '/' in raw_ans:
                verified_answers[detected_exp_q] = raw_ans

        explanations[detected_exp_q] = exp_text
        detected_exp_q += 1

    return explanations, verified_answers


def main():
    parser = argparse.ArgumentParser(description="JGEEBILS Mock Exam Generator")
    parser.add_argument('--task-dir', type=str, default='.', help='Directory containing the JGEEBILS PDF papers')
    args = parser.parse_args()

    pdf_files = sorted([
        f for f in glob.glob(os.path.join(args.task_dir, '*.pdf'))
        if "sanitized" not in os.path.basename(f)
    ])
    if not pdf_files:
        print(f"No PDFs found in directory: {args.task_dir}")
        return

    # 1. Interactive CLI Menu to select the single exam paper before processing
    print("\n" + "="*55)
    print("  Available JGEEBILS Papers:")
    print("="*55)
    cache_dir = os.path.join(args.task_dir, "cache")
    for idx, pdf in enumerate(pdf_files, 1):
        fname = os.path.basename(pdf)
        base = os.path.splitext(fname)[0]
        cache_path = os.path.join(cache_dir, f"{base}_parsed_cache.js")
        status = "[Cached - Instant Load]" if os.path.exists(cache_path) else "[Unprocessed - Needs Scan]"
        print(f"  [{idx}] {fname:<30} {status}")
    print("="*55)

    choice = input(f"\nSelect paper number to launch (1-{len(pdf_files)}) [or 'a' for all]: ").strip().lower()
    if choice != 'a':
        try:
            sel_idx = int(choice) - 1
            if 0 <= sel_idx < len(pdf_files):
                pdf_files = [pdf_files[sel_idx]]
            else:
                print("Invalid selection. Exiting.")
                return
        except ValueError:
            print("Invalid input. Exiting.")
            return

    papers_data = {}
    question_crops_data = {}
    latex_questions_data = {}
    explanations_data = {}

    # Create localized cache folder inside the task directory
    cache_dir = os.path.join(args.task_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    # Initialize empty dictionaries so the HTML generates properly
    latex_questions_data = {os.path.basename(pdf): {} for pdf in pdf_files}
    
    for pdf in pdf_files:
        filename = os.path.basename(pdf)
        base_name = os.path.splitext(filename)[0]
        cache_crops_file = os.path.join(cache_dir, f"{base_name}_crops.json")

        doc = fitz.open(pdf)

        # 2a. Fast Step: Load or Generate Image Crops instantly
        if os.path.exists(cache_crops_file):
            with open(cache_crops_file, 'r', encoding='utf-8') as cf:
                crops = {int(k): v for k, v in json.load(cf).items()}
        else:
            crops = crop_questions_from_pdf(doc)
            with open(cache_crops_file, 'w', encoding='utf-8') as cf:
                json.dump(crops, cf)
        
        question_crops_data[filename] = crops

        # 2b. Fast Step: Load Explanations and Verified Keys instantly
        exp_file = os.path.join(args.task_dir, f"{base_name}_explanations.md")
        verified_keys = {}
        if os.path.exists(exp_file):
            expls, verified_keys = parse_explanations(exp_file)
            explanations_data[filename] = expls
            print(f"Loaded explanations and verified keys for {filename}")

        if verified_keys:
            papers_data[filename] = verified_keys
            print(f"Prepared {len(verified_keys)} questions for {filename}. Ready to launch exam.")

    if not papers_data:
        print("No papers could be loaded.")
        return

    papers_json = json.dumps(papers_data)
    question_crops_json = json.dumps(question_crops_data)
    latex_questions_json = json.dumps(latex_questions_data)
    explanations_json = json.dumps(explanations_data)

    html_content = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>JGEEBILS Exam Simulator</title>
        
        <!-- MathJax Configuration -->
                <!-- KaTeX CSS & JS for ultra-reliable, instant math rendering -->
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css">
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.js"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/contrib/auto-render.min.js"></script>

        
        <style>
            body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; height: 100vh; overflow: hidden; background: #f5f5f7; }}
            #left-pane {{ flex: 6; height: 100%; border-right: 1px solid #d2d2d7; background: #525659; transition: opacity 0.2s; }}
            iframe {{ filter: grayscale(100%); width: 100%; height: 100%; border: none; }}
            #right-pane {{ flex: 4; height: 100%; display: flex; flex-direction: column; background: #ffffff; }}
            #header {{ padding: 20px; background: #000000; color: white; text-align: center; z-index: 10; display: flex; flex-direction: column; align-items: center; }}
            #timer {{ font-size: 2.5em; font-weight: 600; font-variant-numeric: tabular-nums; margin: 10px 0; }}
            .controls {{ display: flex; gap: 10px; width: 100%; padding: 0 10px; }}
            button {{ padding: 12px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 1em; }}
            #pause-btn {{ background: #ff9f0a; color: white; flex: 1; transition: background 0.2s; }}

            /* Floating Advanced Scientific & Graphing Calculator */
            #calc-toggle-btn {{ background: #34c759; color: white; flex: 1; transition: background 0.2s; }}
            #calc-modal {{ position: fixed; bottom: 25px; left: 25px; width: 360px; background: #1c1c1e; color: #fff; border-radius: 14px; box-shadow: 0 10px 30px rgba(0,0,0,0.45); z-index: 1000; display: none; flex-direction: column; overflow: hidden; border: 1px solid #3a3a3c; }}
            #calc-header {{ background: #2c2c2e; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; font-size: 0.9em; font-weight: 600; cursor: move; }}
            #calc-display {{ background: #000; color: #00ff66; font-family: monospace; font-size: 1.4em; padding: 12px; text-align: right; min-height: 32px; word-break: break-all; }}
            .calc-tabs {{ display: flex; background: #2c2c2e; }}
            .calc-tab {{ flex: 1; padding: 8px; font-size: 0.8em; text-align: center; cursor: pointer; color: #aeaeb2; }}
            .calc-tab.active {{ background: #3a3a3c; color: #fff; font-weight: bold; }}
            .calc-pad {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; padding: 10px; }}
            .calc-btn {{ background: #3a3a3c; color: white; border: none; border-radius: 6px; padding: 10px 4px; font-size: 0.85em; cursor: pointer; }}
            .calc-btn:hover {{ background: #48484a; }}
            .calc-btn.op {{ background: #ff9f0a; }}
            .calc-btn.fn {{ background: #0071e3; }}
            #calc-graph-view {{ display: none; padding: 10px; flex-direction: column; align-items: center; }}
            #calc-graph-canvas {{ background: #111; border-radius: 6px; border: 1px solid #333; }}

            /* Review Filters & Sorting Toolbar */
            .filter-bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0 15px 0; align-items: center; justify-content: space-between; background: #f2f2f7; padding: 10px 14px; border-radius: 10px; }}
            .filter-group {{ display: flex; gap: 6px; }}
            .filter-pill {{ padding: 6px 12px; border-radius: 16px; border: 1px solid #c7c7cc; background: white; font-size: 0.85em; cursor: pointer; font-weight: 600; color: #3a3a3c; }}
            .filter-pill.active {{ background: #0071e3; color: white; border-color: #0071e3; }}
            .sort-select {{ padding: 6px 10px; border-radius: 8px; border: 1px solid #c7c7cc; background: white; font-size: 0.85em; font-weight: 500; }}

            #selector-screen {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: white; z-index: 50; display: flex; flex-direction: column; align-items: center; justify-content: center; }}
            .paper-btn {{ margin: 10px; padding: 15px 30px; font-size: 1.2em; background: #2c2c2e; color: white; border-radius: 8px; width: 350px; text-align: left; }}
            .paper-btn:hover {{ background: #0071e3; }}

            #omr-container {{ flex: 1; overflow-y: auto; padding: 20px; transition: opacity 0.2s; }}
            .question-row {{ margin-bottom: 12px; padding: 12px; background: #fbfbfd; border-radius: 8px; border: 1px solid #e5e5ea; display: flex; justify-content: space-between; align-items: center; }}
            .q-num {{ font-weight: 600; width: 40px; color: #1d1d1f; }}
            .options label {{ margin-right: 12px; cursor: pointer; user-select: none; }}
            #submit-btn {{ padding: 18px; background: #0071e3; color: white; font-size: 1.2em; transition: background 0.2s; flex-shrink: 0; }}
            #submit-btn:hover {{ background: #0077ed; }}

            #results {{ display: none; padding: 30px; flex: 1; overflow-y: auto; text-align: center; }}
            .score-card {{ background: #f2f2f7; padding: 20px; border-radius: 12px; margin-top: 20px; }}
            .action-btn-row {{ display: flex; gap: 10px; margin-top: 15px; }}
            .export-btn {{ flex: 1; padding: 12px; font-size: 0.95em; border-radius: 8px; color: white; background: #34c759; }}
            .export-btn:hover {{ opacity: 0.9; }}
            .pdf-btn {{ background: #5856d6; }}

            #review-container {{ margin-top: 30px; text-align: left; }}
            .review-item {{ padding: 22px; margin-bottom: 24px; border-radius: 10px; background: #fff; border: 1px solid #d2d2d7; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
            .review-item h4 {{ margin: 0 0 14px 0; color: #1d1d1f; border-bottom: 2px solid #0071e3; padding-bottom: 6px; font-size: 1.15em; }}

            .section-label {{ font-size: 0.82em; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 700; color: #86868b; margin-bottom: 8px; margin-top: 14px; }}
            .section-label:first-of-type {{ margin-top: 0; }}

            .question-crop-img {{ max-width: 100%; border: 1px solid #e5e5ea; border-radius: 6px; display: block; margin-bottom: 8px; background: white; }}
            .latex-toggle {{ font-size: 0.85em; color: #0071e3; cursor: pointer; margin-top: 6px; user-select: none; }}
            .latex-content {{ padding: 12px; background: #fbfbfd; border: 1px solid #e5e5ea; border-radius: 6px; margin-top: 8px; font-size: 0.95em; line-height: 1.5; }}

            .review-solution-box {{ background: #ffffff; padding: 12px 14px; border-radius: 6px; font-size: 0.95em; border: 1px solid #e5e5ea; display: flex; justify-content: space-between; align-items: center; }}
            .review-exp-box {{ background: #fbfbfd; padding: 16px; border-radius: 6px; font-size: 0.95em; line-height: 1.6; color: #2c2c2e; border: 1px solid #e5e5ea; overflow-x: auto; }}
            .review-exp-box p {{ margin: 0 0 10px 0; }}
            .review-exp-box p:last-child {{ margin-bottom: 0; }}

            @media print {{
                body {{ display: block; overflow: visible; height: auto; background: white; }}
                #left-pane, #selector-screen, #header, #submit-btn, .action-btn-row, #back-menu-btn, .latex-toggle {{ display: none !important; }}
                #right-pane {{ display: block !important; width: 100%; height: auto; }}
                #results {{ display: block !important; padding: 0; }}
                .score-card {{ border: 1px solid #ccc; }}
                .review-item {{ page-break-inside: avoid; border: 1px solid #ccc; }}
                .question-crop-img {{ max-width: 100% !important; }}
                .filter-bar {{ display: none !important; }}
            }}
        </style>
    </head>
    <body>
        <div id="selector-screen">
            <h1 style="margin-bottom: 30px;">Select a JGEEBILS Paper</h1>
            <div id="paper-list"></div>
        </div>

        <div id="left-pane" style="display:none;">
            <iframe id="pdf-frame" src=""></iframe>
        </div>

        <div id="right-pane" style="display:none;">
            <div id="header">
                <div style="font-size: 0.9em; text-transform: uppercase; letter-spacing: 1px;">Time Remaining</div>
                <div id="timer">140:00</div>
                <div class="controls">
                    <button id="pause-btn" onclick="togglePause()">Pause Exam</button>
                    <button id="calc-toggle-btn" onclick="toggleCalculator()">Calculator</button>
                </div>
            </div>
            <div id="omr-container">
                <form id="omr-form"></form>
            </div>
            <button id="submit-btn" onclick="submitExam()">Submit Exam</button>
            <div id="results">
                <h2>Exam Results</h2>
                <div class="score-card">
                    <h1 id="final-score" style="font-size: 3em; margin: 10px 0; color: #0071e3;"></h1>
                    <p>Attempted: <span id="res-attempted"></span> / 60</p>
                    <p>Correct: <span id="res-correct" style="color: #34c759; font-weight: bold;"></span></p>
                    <p>Wrong: <span id="res-wrong" style="color: #ff3b30; font-weight: bold;"></span></p>

                    <div class="action-btn-row">
                        <button class="export-btn" onclick="exportResultCSV()">Export result.csv</button>
                        <button class="export-btn pdf-btn" onclick="exportAnalysisPDF()">Export Analysis PDF</button>
                    </div>
                </div>

                <div class="filter-bar">
                    <div class="filter-group">
                        <button class="filter-pill active" onclick="applyFilter('all', this)">All (60)</button>
                        <button class="filter-pill" onclick="applyFilter('Correct', this)">Correct</button>
                        <button class="filter-pill" onclick="applyFilter('Wrong', this)">Wrong</button>
                        <button class="filter-pill" onclick="applyFilter('Unattempted', this)">Unattempted</button>
                    </div>
                    <div>
                        <select class="sort-select" onchange="applySorting(this.value)">
                            <option value="q-asc">Sort: Question (1 &rarr; 60)</option>
                            <option value="q-desc">Sort: Question (60 &rarr; 1)</option>
                            <option value="marks-desc">Marks: High to Low</option>
                            <option value="marks-asc">Marks: Low to High</option>
                        </select>
                    </div>
                </div>

                <div id="review-container"></div>
                <button id="back-menu-btn" onclick="location.reload()" style="margin-top: 20px; padding: 12px 20px; background: #e5e5ea; width: 100%;">Back to Menu</button>
            </div>
        </div>

        <!-- Advanced Scientific + Graphing Calculator Modal -->
        <div id="calc-modal">
            <div id="calc-header">
                <span>Scientific & Graphing Tool</span>
                <span style="cursor:pointer; font-size: 1.2em;" onclick="toggleCalculator()">&times;</span>
            </div>
            <div id="calc-display">0</div>
            <div class="calc-tabs">
                <div class="calc-tab active" id="tab-sci" onclick="switchCalcTab('sci')">Scientific</div>
                <div class="calc-tab" id="tab-graph" onclick="switchCalcTab('graph')">Graph f(x)</div>
            </div>
            <div id="calc-sci-view">
                <div class="calc-pad">
                    <button class="calc-btn fn" onclick="calcFn('sin')">sin</button>
                    <button class="calc-btn fn" onclick="calcFn('cos')">cos</button>
                    <button class="calc-btn fn" onclick="calcFn('tan')">tan</button>
                    <button class="calc-btn fn" onclick="calcFn('ln')">ln</button>
                    <button class="calc-btn fn" onclick="calcFn('log')">log</button>
                    
                    <button class="calc-btn fn" onclick="calcInput('Math.PI')">π</button>
                    <button class="calc-btn fn" onclick="calcFn('sqrt')">√</button>
                    <button class="calc-btn fn" onclick="calcInput('**')">xʸ</button>
                    <button class="calc-btn" onclick="calcInput('(')">(</button>
                    <button class="calc-btn" onclick="calcInput(')')">)</button>
                    
                    <button class="calc-btn" onclick="calcInput('7')">7</button>
                    <button class="calc-btn" onclick="calcInput('8')">8</button>
                    <button class="calc-btn" onclick="calcInput('9')">9</button>
                    <button class="calc-btn op" onclick="calcInput('/')">÷</button>
                    <button class="calc-btn" style="background:#ff3b30;" onclick="calcClear()">C</button>
                    
                    <button class="calc-btn" onclick="calcInput('4')">4</button>
                    <button class="calc-btn" onclick="calcInput('5')">5</button>
                    <button class="calc-btn" onclick="calcInput('6')">6</button>
                    <button class="calc-btn op" onclick="calcInput('*')">×</button>
                    <button class="calc-btn" onclick="calcBackspace()">⌫</button>
                    
                    <button class="calc-btn" onclick="calcInput('1')">1</button>
                    <button class="calc-btn" onclick="calcInput('2')">2</button>
                    <button class="calc-btn" onclick="calcInput('3')">3</button>
                    <button class="calc-btn op" onclick="calcInput('-')">−</button>
                    <button class="calc-btn op" style="grid-row: span 2; height: 100%;" onclick="calcEquals()">=</button>
                    
                    <button class="calc-btn" style="grid-column: span 2;" onclick="calcInput('0')">0</button>
                    <button class="calc-btn" onclick="calcInput('.')">.</button>
                    <button class="calc-btn op" onclick="calcInput('+')">+</button>
                </div>
            </div>
            <div id="calc-graph-view">
                <input id="graph-expr" type="text" placeholder="f(x) e.g. sin(x), x^2 - 4" style="width: 90%; padding: 6px; margin: 6px 0; border-radius: 4px; border: 1px solid #444; background: #222; color: #fff; font-size: 0.85em;" />
                <button class="calc-btn fn" style="width: 94%; margin-bottom: 8px;" onclick="plotFunction()">Plot Curve</button>
                <canvas id="calc-graph-canvas" width="320" height="180"></canvas>
            </div>
        </div>

        <script>
            const papersData = {papers_json};
            const questionCropsData = {question_crops_json};
            const latexQuestionsData = {latex_questions_json};
            const explanationsData = {explanations_json};

            let activeAnswerKey = {{}};
            let activeFilename = "";
            let timeLeft = 140 * 60;
            let timerInterval;
            let isPaused = false;
            let examResultsData = [];

            const listDiv = document.getElementById('paper-list');
            for (const [filename, key] of Object.entries(papersData)) {{
                let btn = document.createElement('button');
                btn.className = 'paper-btn';
                btn.innerHTML = `${{filename}} <br><span style="font-size: 0.8em; color: #a1a1a6;">${{explanationsData[filename] ? 'Includes Post-Exam Analysis' : 'No Explanations found'}}</span>`;
                btn.onclick = () => startExam(filename, key);
                listDiv.appendChild(btn);
            }}

            function handleRadioClick(radio) {{
                if (radio.dataset.wasChecked === "true") {{
                    radio.checked = false;
                    radio.dataset.wasChecked = "false";
                }} else {{
                    const group = document.getElementsByName(radio.name);
                    for (const r of group) {{
                        r.dataset.wasChecked = "false";
                    }}
                    radio.dataset.wasChecked = "true";
                }}
            }}

            function startExam(filename, key) {{
                activeAnswerKey = key;
                activeFilename = filename;
                document.getElementById('selector-screen').style.display = 'none';
                document.getElementById('left-pane').style.display = 'block';
                document.getElementById('right-pane').style.display = 'flex';

                document.getElementById('pdf-frame').src = encodeURIComponent(filename);

                let omr = document.getElementById('omr-form');
                omr.innerHTML = '';
                for (let i = 1; i <= 60; i++) {{
                    omr.innerHTML += `
                        <div class="question-row">
                            <div class="q-num">Q${{i}}</div>
                            <div class="options">
                                <label><input type="radio" name="q${{i}}" value="a" onclick="handleRadioClick(this)"> A</label>
                                <label><input type="radio" name="q${{i}}" value="b" onclick="handleRadioClick(this)"> B</label>
                                <label><input type="radio" name="q${{i}}" value="c" onclick="handleRadioClick(this)"> C</label>
                                <label><input type="radio" name="q${{i}}" value="d" onclick="handleRadioClick(this)"> D</label>
                            </div>
                        </div>
                    `;
                }}

                timerInterval = setInterval(updateTimer, 1000);
            }}

            function togglePause() {{
                isPaused = !isPaused;
                let btn = document.getElementById('pause-btn');
                btn.innerText = isPaused ? "Resume Exam" : "Pause Exam";
                btn.style.background = isPaused ? "#34c759" : "#ff9f0a";

                document.getElementById('left-pane').style.opacity = isPaused ? "0" : "1";
                document.getElementById('omr-container').style.opacity = isPaused ? "0" : "1";
            }}

            function updateTimer() {{
                if (isPaused) return;
                let m = Math.floor(timeLeft / 60);
                let s = Math.floor(timeLeft % 60);
                document.getElementById('timer').innerText = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
                if (timeLeft <= 0) submitExam();
                timeLeft--;
            }}

                        function renderMath() {{
                if (typeof renderMathInElement === 'function') {{
                    const container = document.getElementById('review-container');
                    if (container) {{
                        renderMathInElement(container, {{
                            delimiters: [
                                {{left: '$$', right: '$$', display: true}},
                                {{left: '$', right: '$', display: false}},
                                {{left: '\\(', right: '\\)', display: false}},
                                {{left: '\\[', right: '\\]', display: true}}
                            ],
                            throwOnError: false
                        }});
                    }}
                }} else {{
                    setTimeout(renderMath, 100);
                }}
            }}

            // --- Scientific & Graphing Calculator Logic ---
            let calcExpr = "";
            function toggleCalculator() {{
                const modal = document.getElementById('calc-modal');
                modal.style.display = (modal.style.display === 'flex') ? 'none' : 'flex';
            }}
            function switchCalcTab(tab) {{
                document.getElementById('tab-sci').classList.toggle('active', tab === 'sci');
                document.getElementById('tab-graph').classList.toggle('active', tab === 'graph');
                document.getElementById('calc-sci-view').style.display = (tab === 'sci') ? 'block' : 'none';
                document.getElementById('calc-graph-view').style.display = (tab === 'graph') ? 'flex' : 'none';
            }}
            function calcInput(char) {{
                if (calcExpr === "0" && !isNaN(char)) calcExpr = "";
                calcExpr += char;
                document.getElementById('calc-display').innerText = calcExpr || "0";
            }}
            function calcClear() {{
                calcExpr = "";
                document.getElementById('calc-display').innerText = "0";
            }}
            function calcBackspace() {{
                calcExpr = calcExpr.slice(0, -1);
                document.getElementById('calc-display').innerText = calcExpr || "0";
            }}
            function calcFn(fn) {{
                if (fn === 'sin') calcExpr += 'Math.sin(';
                else if (fn === 'cos') calcExpr += 'Math.cos(';
                else if (fn === 'tan') calcExpr += 'Math.tan(';
                else if (fn === 'ln') calcExpr += 'Math.log(';
                else if (fn === 'log') calcExpr += 'Math.log10(';
                else if (fn === 'sqrt') calcExpr += 'Math.sqrt(';
                document.getElementById('calc-display').innerText = calcExpr;
            }}
            function calcEquals() {{
                try {{
                    let sanitized = calcExpr.replace(/Math\\.PI/g, Math.PI);
                    let res = Function('"use strict";return (' + sanitized + ')')();
                    calcExpr = String(Math.round(res * 1e8) / 1e8);
                    document.getElementById('calc-display').innerText = calcExpr;
                }} catch(e) {{
                    document.getElementById('calc-display').innerText = "Error";
                    calcExpr = "";
                }}
            }}
            function plotFunction() {{
                const canvas = document.getElementById('calc-graph-canvas');
                const ctx = canvas.getContext('2d');
                const expr = document.getElementById('graph-expr').value.trim();
                ctx.clearRect(0, 0, canvas.width, canvas.height);

                // Draw Axes
                const w = canvas.width, h = canvas.height;
                const ox = w / 2, oy = h / 2;
                ctx.strokeStyle = "#444";
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(0, oy); ctx.lineTo(w, oy);
                ctx.moveTo(ox, 0); ctx.lineTo(ox, h);
                ctx.stroke();

                if (!expr) return;
                // Parse and draw f(x)
                let safeExpr = expr
                    .replace(/\\bsin\\b/g, 'Math.sin')
                    .replace(/\\bcos\\b/g, 'Math.cos')
                    .replace(/\\btan\\b/g, 'Math.tan')
                    .replace(/\\bln\\b/g, 'Math.log')
                    .replace(/\\^/g, '**');

                ctx.strokeStyle = "#34c759";
                ctx.lineWidth = 2;
                ctx.beginPath();
                let started = false;
                const scale = 25; // 25 px per 1 unit

                for (let px = 0; px < w; px++) {{
                    let x = (px - ox) / scale;
                    try {{
                        let y = Function('x', '"use strict";return (' + safeExpr + ')')(x);
                        let py = oy - (y * scale);
                        if (!isNaN(py) && isFinite(py)) {{
                            if (!started) {{ ctx.moveTo(px, py); started = true; }}
                            else {{ ctx.lineTo(px, py); }}
                        }} else {{
                            started = false;
                        }}
                    }} catch(err) {{
                        started = false;
                    }}
                }}
                ctx.stroke();
            }}

            // --- Post-Exam Filtering and Sorting Engine ---
            let currentFilter = 'all';
            let currentSort = 'q-asc';

            function renderReviewItems() {{
                let container = document.getElementById('review-container');
                let items = [...examResultsData];

                // 1. Filter
                if (currentFilter !== 'all') {{
                    items = items.filter(it => it.status === currentFilter);
                }}

                // 2. Sort
                items.sort((a, b) => {{
                    if (currentSort === 'q-asc') return a.question - b.question;
                    if (currentSort === 'q-desc') return b.question - a.question;
                    if (currentSort === 'marks-desc') return b.marks - a.marks;
                    if (currentSort === 'marks-asc') return a.marks - b.marks;
                    return 0;
                }});

                let reviewHTML = `<h3 style="margin-bottom:15px;">Detailed Analysis & Review (${{items.length}} questions shown)</h3>`;

                items.forEach(it => {{
                    reviewHTML += `
                        <div class="review-item" id="review-q-${{it.question}}">
                            <h4>Question ${{it.question}}</h4>
                            <div class="section-label">Question:</div>
                            <div class="review-question-wrap">
                                ${{it.questionMediaHTML}}
                                ${{it.latexToggleHTML}}
                            </div>
                            <div class="section-label">Solution:</div>
                            <div class="review-solution-box">
                                <div><strong>Your Answer:</strong> <span style="color:${{it.color}}; font-weight:bold;">${{it.marked}}</span> &nbsp;|&nbsp; <strong>Correct Answer:</strong> <span style="color:#0071e3; font-weight:bold;">${{it.correct}}</span></div>
                                <div><span style="font-weight:600; color:${{it.color}};">${{it.status}}</span> (${{it.marks > 0 ? '+' + it.marks : it.marks}} marks)</div>
                            </div>
                            <div class="section-label">Detailed Analysis:</div>
                            <div class="review-exp-box">${{it.expText}}</div>
                        </div>
                    `;
                }});

                container.innerHTML = reviewHTML;
                renderMath();
            }}

            function applyFilter(filterType, btnEl) {{
                currentFilter = filterType;
                document.querySelectorAll('.filter-pill').forEach(el => el.classList.remove('active'));
                if (btnEl) btnEl.classList.add('active');
                renderReviewItems();
            }}

            function applySorting(sortType) {{
                currentSort = sortType;
                renderReviewItems();
            }}


            function submitExam() {{
                clearInterval(timerInterval);
                document.getElementById('omr-container').style.display = 'none';
                document.getElementById('submit-btn').style.display = 'none';
                document.getElementById('header').innerHTML = '<h2>Fetching Background Analysis...</h2>';
                document.getElementById('results').style.display = 'flex';
                document.getElementById('left-pane').style.opacity = "1";

                // Dynamically inject the background-processed LLaVA cache script
                const cleanBase = activeFilename.replace(/\\.pdf$/i, "");
                let script = document.createElement('script');
                script.src = `cache/${{cleanBase}}_parsed_cache.js`;
                
                script.onload = function() {{
                    if (window.LATE_CACHE) {{
                        activeAnswerKey = Object.assign({{}}, window.LATE_CACHE.ollama_keys, activeAnswerKey);
                        latexQuestionsData[activeFilename] = window.LATE_CACHE.latex_qs;
                    }}
                    generateReview();
                }};
                
                script.onerror = function() {{
                    // Fallback if background scan isn't done or failed
                    generateReview();
                }};
                
                document.body.appendChild(script);
            }}

            function generateReview() {{
                document.getElementById('header').style.display = 'none';
                let score = 0, correct = 0, wrong = 0, attempted = 0;

                let expl = explanationsData[activeFilename] || {{}};
                let crops = questionCropsData[activeFilename] || {{}};
                let latexQs = latexQuestionsData[activeFilename] || {{}};

                examResultsData = [];

                for (let i = 1; i <= 60; i++) {{
                    let radios = document.getElementsByName('q' + i);
                    let selected = null;
                    for (let r of radios) {{ if (r.checked) selected = r.value; r.disabled = true; }}

                    let trueAns = activeAnswerKey[i] ? activeAnswerKey[i].toLowerCase() : null;
                    let weight = (i <= 40) ? 1 : 2;
                    let penalty = 0.5;
                    let marksGained = 0;
                    let status = "Unattempted";

                    let isCorrect = false;
                    if (selected && trueAns) {{
                        let accepted = trueAns.split('/');
                        isCorrect = accepted.includes(selected);
                    }}

                    if (selected) {{
                        attempted++;
                        if (isCorrect) {{
                            score += weight;
                            correct++;
                            marksGained = weight;
                            status = "Correct";
                        }} else {{
                            score -= penalty;
                            wrong++;
                            marksGained = -penalty;
                            status = "Wrong";
                        }}
                    }}

                    let userAnsDisp = selected ? selected.toUpperCase() : "Unattempted";
                    let correctAnsDisp = trueAns ? trueAns.toUpperCase() : "N/A";
                    let color = isCorrect ? '#34c759' : (userAnsDisp === "Unattempted" ? '#8e8e93' : '#ff3b30');

                    let questionMediaHTML = '';
                    let cropImages = crops[i] || [];
                    if (cropImages.length > 0) {{
                        cropImages.forEach(imgData => {{
                            questionMediaHTML += `<img src="${{imgData}}" class="question-crop-img" alt="Question ${{i}}" />`;
                        }});
                    }} else {{
                        questionMediaHTML = `<div style="padding:10px; background:#f4f5f7; border-radius:6px; color:#555;">Refer to Question ${{i}} in the official PDF.</div>`;
                    }}

                    let latexText = latexQs[i];
                    let latexToggleHTML = '';
                    if (latexText) {{
                        latexToggleHTML = `
                            <details style="margin-top:8px;">
                                <summary class="latex-toggle">View LaTeX Text & Formulae</summary>
                                <div class="latex-content">${{latexText}}</div>
                            </details>
                        `;
                    }}

                    let expText = expl[i] ? expl[i] : "No explanation provided for this question.";

                    examResultsData.push({{
                        question: i,
                        marked: userAnsDisp,
                        correct: correctAnsDisp,
                        status: status,
                        marks: marksGained,
                        color: color,
                        questionMediaHTML: questionMediaHTML,
                        latexToggleHTML: latexToggleHTML,
                        expText: expText
                    }});
                }}

                document.getElementById('final-score').innerText = score.toFixed(1) + " / 80";
                document.getElementById('res-attempted').innerText = attempted;
                document.getElementById('res-correct').innerText = correct;
                document.getElementById('res-wrong').innerText = wrong;

                renderReviewItems();
            }}

            function exportResultCSV() {{
                if (!examResultsData || examResultsData.length === 0) return;
                let csvContent = "data:text/csv;charset=utf-8,";
                csvContent += "Question,Marked Option,Correct Option,Status,Marks\\n";

                examResultsData.forEach(row => {{
                    csvContent += `${{row.question}},${{row.marked}},${{row.correct}},${{row.status}},${{row.marks}}\\n`;
                }});

                const encodedUri = encodeURI(csvContent);
                const link = document.createElement("a");
                const cleanBase = activeFilename.replace(/\\.pdf$/i, "");
                link.setAttribute("href", encodedUri);
                link.setAttribute("download", `${{cleanBase}}_result.csv`);
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            }}

            function exportAnalysisPDF() {{
                window.print();
            }}
        </script>
    </body>
    </html>"""

    html_path = os.path.join(args.task_dir, "mock_interface.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Generated {html_path}")
    webbrowser.open('file://' + os.path.realpath(html_path))
    
    # --- BACKGROUND LLaVA PROCESSING ---
    for pdf in pdf_files:
        filename = os.path.basename(pdf)
        base_name = os.path.splitext(filename)[0]
        cache_js_file = os.path.join(cache_dir, f"{base_name}_parsed_cache.js")
        
        if not os.path.exists(cache_js_file):
            print(f"\n[Background Task] Starting LLaVA LaTeX scan for {filename}...")
            print("You can start your exam in the browser now. This will process silently in the background.")
            
            doc = fitz.open(pdf)
            ollama_keys, latex_qs = parse_pdf_with_ollama(doc)
            
            # Save results as a Javascript file so the browser can load it locally bypassing CORS
            with open(cache_js_file, 'w', encoding='utf-8') as cf:
                cf.write(f"window.LATE_CACHE = {json.dumps({'ollama_keys': ollama_keys, 'latex_qs': latex_qs})};")
            print(f"\n[Background Task] {filename} scan complete! Data cached for your post-exam analysis.")


if __name__ == "__main__":
    main()
