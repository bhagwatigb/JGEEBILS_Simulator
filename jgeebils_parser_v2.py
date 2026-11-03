import fitz  # PyMuPDF
import json
import os
import webbrowser
import argparse
import glob
import re

def parse_pdf_for_answers(pdf_path):
    print(f"Parsing {os.path.basename(pdf_path)} for answers...")
    doc = fitz.open(pdf_path)
    answer_key = {}
    detected_q = 1

    for page in doc:
        blocks = page.get_text("dict").get("blocks", [])
        for b in blocks:
            if b.get("type") == 0:
                for l in b.get("lines", []):
                    for s in l.get("spans", []):
                        color = s.get("color", 0)
                        r = (color >> 16) & 0xFF
                        g = (color >> 8) & 0xFF
                        b_col = color & 0xFF
                        
                        if r > 150 and g < 100 and b_col < 100:
                            text = s["text"].strip().lower()
                            if len(text) > 0 and text[0] in ['a', 'b', 'c', 'd']:
                                answer_key[detected_q] = text[0]
                                detected_q += 1
    return answer_key

def parse_explanations(md_path):
    print(f"Parsing explanations from {os.path.basename(md_path)}...")
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Matches question blocks and stops before the next divider, header, or question
    pattern = r"### Question\s+(\d+).*?\*\*Explanation:\*\*\s*(.*?)(?=\n---|\n### Question|\Z)"
    matches = re.finditer(pattern, content, re.DOTALL | re.IGNORECASE)
    
    explanations = {}
    detected_exp_q = 1
    for m in matches:
        exp_text = m.group(2).strip().replace('\n', '<br>')
        explanations[detected_exp_q] = exp_text
        detected_exp_q += 1
        
    return explanations

def main():
    parser = argparse.ArgumentParser(description="JGEEBILS Mock Exam Generator")
    parser.add_argument('--task-dir', type=str, default='.', help='Directory containing the JGEEBILS PDF papers')
    args = parser.parse_args()

    pdf_files = glob.glob(os.path.join(args.task_dir, '*.pdf'))
    if not pdf_files:
        print(f"No PDFs found in directory: {args.task_dir}")
        return

    papers_data = {}
    explanations_data = {}
    
    for pdf in pdf_files:
        filename = os.path.basename(pdf)
        if "sanitized" in filename:
            continue
        
        ans_key = parse_pdf_for_answers(pdf)
        if ans_key:
            papers_data[filename] = ans_key
            print(f"Extracted {len(ans_key)} answers from {filename}")
            
            base_name = os.path.splitext(filename)[0]
            exp_file = os.path.join(args.task_dir, f"{base_name}_explanations.md")
            if os.path.exists(exp_file):
                explanations_data[filename] = parse_explanations(exp_file)
                print(f"Loaded explanations for {filename}")

    if not papers_data:
        print("No answer keys could be extracted from the PDFs.")
        return

    papers_json = json.dumps(papers_data)
    explanations_json = json.dumps(explanations_data)

    html_content = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>JGEEBILS Exam Simulator</title>
        <!-- Load MathJax with inline dollar delimiters -->
        <script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script>
        <script>
            MathJax = {{
                tex: {{
                    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
                    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']]
                }}
            }};
        </script>
        <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
        <style>
            body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex; height: 100vh; overflow: hidden; background: #f5f5f7; }}
            #left-pane {{ flex: 6; height: 100%; border-right: 1px solid #d2d2d7; background: #525659; transition: opacity 0.2s; }}
            iframe {{ filter: grayscale(100%); width: 100%; height: 100%; border: none; }}
            #right-pane {{ flex: 4; height: 100%; display: flex; flex-direction: column; background: #ffffff; }}
            #header {{ padding: 20px; background: #000000; color: white; text-align: center; z-index: 10; display: flex; flex-direction: column; align-items: center; }}
            #timer {{ font-size: 2.5em; font-weight: 600; font-variant-numeric: tabular-nums; margin: 10px 0; }}
            .controls {{ display: flex; gap: 10px; width: 100%; padding: 0 10px; }}
            button {{ padding: 12px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 1em; }}
            #pause-btn {{ background: #ff9f0a; color: white; flex: 1; transition: background 0.2s; }}
            
            #selector-screen {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: white; z-index: 50; display: flex; flex-direction: column; align-items: center; justify-content: center; }}
            .paper-btn {{ margin: 10px; padding: 15px 30px; font-size: 1.2em; background: #2c2c2e; color: white; border-radius: 8px; width: 350px; text-align: left; }}
            .paper-btn:hover {{ background: #0071e3; }}
            
            #omr-container {{ flex: 1; overflow-y: auto; padding: 20px; transition: opacity 0.2s; }}
            .question-row {{ margin-bottom: 12px; padding: 12px; background: #fbfbfd; border-radius: 8px; border: 1px solid #e5e5ea; display: flex; justify-content: space-between; }}
            .q-num {{ font-weight: 600; width: 40px; color: #1d1d1f; }}
            .options label {{ margin-right: 12px; cursor: pointer; }}
            #submit-btn {{ padding: 18px; background: #0071e3; color: white; font-size: 1.2em; transition: background 0.2s; flex-shrink: 0; }}
            #submit-btn:hover {{ background: #0077ed; }}
            
            #results {{ display: none; padding: 30px; flex: 1; overflow-y: auto; text-align: center; }}
            .score-card {{ background: #f2f2f7; padding: 20px; border-radius: 12px; margin-top: 20px; }}
            
            #review-container {{ margin-top: 30px; text-align: left; }}
            .review-item {{ padding: 20px; margin-bottom: 15px; border-radius: 8px; background: #fff; border: 1px solid #d2d2d7; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
            .review-item h4 {{ margin: 0 0 10px 0; color: #1d1d1f; border-bottom: 1px solid #e5e5ea; padding-bottom: 5px; }}
            .review-exp {{ background: #fbfbfd; padding: 15px; border-radius: 6px; margin-top: 10px; font-size: 0.95em; line-height: 1.5; color: #333; overflow-x: auto; }}
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
                </div>
                <div id="review-container"></div>
                <button onclick="location.reload()" style="margin-top: 20px; padding: 12px 20px; background: #e5e5ea; width: 100%;">Back to Menu</button>
            </div>
        </div>

        <script>
            const papersData = {papers_json};
            const explanationsData = {explanations_json};
            
            let activeAnswerKey = {{}};
            let activeFilename = "";
            let timeLeft = 140 * 60;
            let timerInterval;
            let isPaused = false;
            
            const listDiv = document.getElementById('paper-list');
            for (const [filename, key] of Object.entries(papersData)) {{
                let btn = document.createElement('button');
                btn.className = 'paper-btn';
                btn.innerHTML = `${{filename}} <br><span style="font-size: 0.8em; color: #a1a1a6;">${{explanationsData[filename] ? 'Includes Post-Exam Analysis' : 'No Explanations found'}}</span>`;
                btn.onclick = () => startExam(filename, key);
                listDiv.appendChild(btn);
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
                    omr.innerHTML += `<div class="question-row"><div class="q-num">Q${{i}}</div><div class="options"><label><input type="radio" name="q${{i}}" value="a"> A</label><label><input type="radio" name="q${{i}}" value="b"> B</label><label><input type="radio" name="q${{i}}" value="c"> C</label><label><input type="radio" name="q${{i}}" value="d"> D</label></div></div>`;
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

            function submitExam() {{
                clearInterval(timerInterval);
                document.getElementById('omr-container').style.display = 'none';
                document.getElementById('submit-btn').style.display = 'none';
                document.getElementById('header').style.display = 'none';
                document.getElementById('results').style.display = 'flex';
                document.getElementById('left-pane').style.opacity = "1";

                let score = 0, correct = 0, wrong = 0, attempted = 0;
                let reviewHTML = '<h3>Detailed Analysis</h3>';
                let expl = explanationsData[activeFilename] || {{}};

                for (let i = 1; i <= 60; i++) {{
                    let radios = document.getElementsByName('q' + i);
                    let selected = null;
                    for (let r of radios) {{ if (r.checked) selected = r.value; r.disabled = true; }}
                    
                    let trueAns = activeAnswerKey[i] ? activeAnswerKey[i].toLowerCase() : null;
                    let weight = (i <= 40) ? 1 : 2;
                    let penalty = 0.5;

                    if (selected) {{
                        attempted++;
                        if (selected === trueAns) {{ score += weight; correct++; }}
                        else {{ score -= penalty; wrong++; }}
                    }}
                    
                    let userAnsDisp = selected ? selected.toUpperCase() : "Unattempted";
                    let correctAnsDisp = trueAns ? trueAns.toUpperCase() : "N/A";
                    let isCorrect = (userAnsDisp === correctAnsDisp);
                    let color = isCorrect ? '#34c759' : (userAnsDisp === "Unattempted" ? '#8e8e93' : '#ff3b30');
                    let expText = expl[i] ? expl[i] : "No explanation provided for this question.";

                    reviewHTML += `
                        <div class="review-item">
                            <h4>Question ${{i}}</h4>
                            <p><strong>Your Answer:</strong> <span style="color:${{color}}; font-weight:bold;">${{userAnsDisp}}</span> | <strong>Correct Answer:</strong> ${{correctAnsDisp}}</p>
                            <div class="review-exp"><strong>Explanation:</strong><br/><br/>${{expText}}</div>
                        </div>
                    `;
                }}
                
                document.getElementById('final-score').innerText = score.toFixed(1) + " / 80";
                document.getElementById('res-attempted').innerText = attempted;
                document.getElementById('res-correct').innerText = correct;
                document.getElementById('res-wrong').innerText = wrong;
                
                document.getElementById('review-container').innerHTML = reviewHTML;
                
                if (window.MathJax) {{
                    MathJax.typesetPromise();
                }}
            }}
        </script>
    </body>
    </html>"""
    
    html_path = os.path.join(args.task_dir, "mock_interface.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    print(f"Generated {html_path}")
    webbrowser.open('file://' + os.path.realpath(html_path))

if __name__ == "__main__":
    main()
