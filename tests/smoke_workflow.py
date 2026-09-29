import os, sys, io, sqlite3, zipfile
os.environ['OLC_INSTANCE'] = sys.argv[1]
sys.path.insert(0, '.')
from app import app
from werkzeug.security import generate_password_hash

db = sqlite3.connect(os.path.join(sys.argv[1], 'olc.db'))
db.execute("INSERT INTO users(username, display_name, role, clearance, pw_hash, created_at) VALUES ('viewer1','Public Viewer','viewer','PUBLIC',?, 'now')", (generate_password_hash('x' * 12),))
db.commit()
vid = db.execute("select id from users where username='viewer1'").fetchone()[0]
c = app.test_client()


def login(uid):
    with c.session_transaction() as s:
        s.clear(); s['uid'] = uid; s['csrf'] = 't'


def ok(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg)


login(1)
r = c.post('/indicator/INS-01/review', data={'review_status': 'IN REVIEW'})
ok(r.status_code == 400, f'CSRF enforced ({r.status_code})')
e = db.execute("select id, evidence_code from evidence where indicator_id='INS-03' and origin like 'ai%' order by match_score desc").fetchone()
r = c.post(f'/evidence/{e[0]}', data={'csrf': 't', 'act': 'approve', 'reviewer_notes': 'Confirmed memo adopted'})
ok(r.status_code == 302, 'approve evidence')
c.post(f'/evidence/{e[0]}', data={'csrf': 't', 'act': 'include'})
row = db.execute("select review_status, workflow_stage, include_in_submission, reviewer from evidence where id=?", (e[0],)).fetchone()
ok(row[0] == 'APPROVED' and row[1] == 'INCLUDED IN SUBMISSION', f'workflow stage {row}')
e2 = db.execute("select id from evidence where indicator_id='INS-03' and origin='matrix'").fetchone()
c.post(f'/evidence/{e2[0]}', data={'csrf': 't', 'act': 'reject', 'reviewer_notes': 'No passage'})
ok(db.execute("select review_status from evidence where id=?", (e2[0],)).fetchone()[0] == 'REJECTED', 'reject evidence')
c.post('/indicator/INS-03/review', data={'csrf': 't', 'review_status': 'IN REVIEW', 'human_prelim_score': '1', 'official_score': '1', 'notes': 'Process documented; need minutes'})
rv = db.execute("select review_status, official_score, human_prelim_score from indicator_reviews where indicator_id='INS-03'").fetchone()
ok(tuple(rv) == ('APPROVED', 1, 1), f'official score recorded {tuple(rv)}')
t = c.get('/submission/INS-03').data.decode()
ok(e[1] in t and 'Cannot draft' not in t, 'submission draft cites approved evidence')
ok('Cannot draft' in c.get('/submission/INS-05').data.decode(), 'indicator without approved evidence refuses to draft')

doc = db.execute("select document_id from evidence where id=?", (e[0],)).fetchone()[0]
before = db.execute("select count(*) from evidence where document_id=?", (doc,)).fetchone()[0]
c.post(f'/source/{doc}/update', data={'csrf': 't', 'act': 'reprocess'})
row2 = db.execute("select review_status, include_in_submission from evidence where id=?", (e[0],)).fetchone()
ok(tuple(row2) == ('APPROVED', 1), f'reprocess preserved human approval {tuple(row2)}')
after = db.execute("select count(*) from evidence where document_id=?", (doc,)).fetchone()[0]
ok(after == before, f'no duplicate evidence after reprocess ({before}->{after})')
c.post(f'/source/{doc}/update', data={'csrf': 't', 'act': 'meta', 'title': 'Distance Learning Scope and Policies', 'doc_type': 'POLICY',
                                      'authority': 'FAU', 'classification': 'PUBLIC', 'doc_date': '2024-02-19', 'reprocess': '1', 'notes': 'test'})
st = db.execute("select count(*) from evidence where document_id=? and ai_status='PROPOSED/DRAFT'", (doc,)).fetchone()[0]
ok(st <= 1, f'un-drafting recalculates unreviewed statuses (drafts left {st})')

rep = (b"Online Student Survey Results 2024-2025\n\nThe 2024-2025 online student survey response rate was 34% (n=1,212). "
       b"82% of online students were satisfied with eTutoring. Based on the survey results, OESS revised the orientation module in Spring 2025.\n")
r = c.post('/sources/add', data={'csrf': 't', 'mode': 'file', 'classification': 'RESTRICTED',
                                 'files': (io.BytesIO(rep), 'online-student-survey-results-2024-25.txt')}, content_type='multipart/form-data')
nd = db.execute("select id, doc_type from documents order by id desc").fetchone()
ok(r.status_code == 302, f'upload txt -> doc {tuple(nd)}')
lv = db.execute("select indicator_id, implementation_level, strength, ai_status, classification from evidence where document_id=?", (nd[0],)).fetchall()
print('    ', lv)
ok(any(l[1] in ('MEASUREMENT', 'IMPROVEMENT') for l in lv), 'report with results classified MEASUREMENT/IMPROVEMENT')
c.post('/sources/add', data={'csrf': 't', 'mode': 'file', 'classification': 'INTERNAL', 'files': (io.BytesIO(rep), 'dup.txt')}, content_type='multipart/form-data')
ok(db.execute("select count(*) from documents where sha256=(select sha256 from documents where id=?)", (nd[0],)).fetchone()[0] == 1, 'duplicate upload detected')
csvb = b"Term,Online retention rate,On-campus retention rate\nFall 2024,78%,81%\nSpring 2025,80%,82%\n"
r = c.post('/sources/add', data={'csrf': 't', 'mode': 'file', 'classification': 'INTERNAL', 'doc_type': 'DATA',
                                 'files': (io.BytesIO(csvb), 'online-retention-dashboard.csv')}, content_type='multipart/form-data')
d2 = db.execute("select id from documents order by id desc").fetchone()[0]
print('    csv ->', db.execute("select indicator_id, implementation_level from evidence where document_id=?", (d2,)).fetchall())
buf = io.BytesIO()
z = zipfile.ZipFile(buf, 'w')
z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Distance Learning Committee Minutes</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Minutes of the March 3, 2025 meeting. The committee approved the proposal to offer the BS in Nursing fully online, '
           'and the Faculty Senate curriculum decision on the online program was recorded.</w:t></w:r></w:p></w:body></w:document>')
z.close()
r = c.post('/sources/add', data={'csrf': 't', 'mode': 'file', 'classification': 'INTERNAL',
                                 'files': (io.BytesIO(buf.getvalue()), 'dl-committee-minutes-2025-03.docx')}, content_type='multipart/form-data')
d3 = db.execute("select id, doc_type, title from documents order by id desc").fetchone()
print('    docx ->', tuple(d3), db.execute("select indicator_id, implementation_level from evidence where document_id=?", (d3[0],)).fetchall())
r = c.post('/sources/add', data={'csrf': 't', 'mode': 'url', 'classification': 'PUBLIC', 'url': 'https://example.com/x'})
ok(b'not on the approved list' in c.get('/sources/add').data or r.status_code == 302, 'non-approved domain refused')

login(vid)
ok(c.get(f'/source/{nd[0]}').status_code == 404, 'PUBLIC viewer cannot open RESTRICTED source')
codes = [r[0] for r in db.execute("select evidence_code from evidence where classification!='PUBLIC'")]
page = c.get('/evidence').data.decode()
ok(codes and not any(f'>{k}<' in page for k in codes), f'PUBLIC viewer evidence list hides {len(codes)} internal/restricted items')
ok(c.post(f'/evidence/{e[0]}', data={'csrf': 't', 'act': 'approve'}).status_code == 403, 'viewer cannot approve')
ok(c.get('/admin').status_code == 403, 'viewer cannot open admin')
login(1)
w = c.get('/commands?cmd=WHAT%20CHANGED').data.decode()
ok('Gaps closed' in w, 'WHAT CHANGED renders')
