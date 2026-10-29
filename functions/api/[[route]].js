// Cloudflare Pages Functions — 단일 라우터 (v2)
// 인증: 앱은 닉네임+PIN 로그인 → Bearer 토큰. PC 워커는 X-Worker-Key.
// 원칙: 서버(D1)가 원본. 앱은 입력마다 홀 단위로 즉시 PUT 하고, 오프라인일 때만 기기에 대기열을 둔다.

const J = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });
const bad = (m, s = 400) => J({ error: m }, s);
const uuid = () => crypto.randomUUID();
const enc = new TextEncoder();
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
const sha256 = async s => hex(await crypto.subtle.digest('SHA-256', enc.encode(s)));
const randHex = n => hex(crypto.getRandomValues(new Uint8Array(n)));
async function pinHash(pin, salt) {
  const key = await crypto.subtle.importKey('raw', enc.encode(pin), 'PBKDF2', false, ['deriveBits']);
  return hex(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: enc.encode(salt), iterations: 10000 }, key, 256));
}
const LOCK_FAILS = 5, LOCK_MIN = 10;

export async function onRequest({ request, env, params, waitUntil }) {
  const path = '/' + (params.route || []).join('/');
  const m = request.method;
  const DB = env.DB;
  const isWorker = !!env.WORKER_KEY && request.headers.get('X-Worker-Key') === env.WORKER_KEY;
  const body = async () => { try { return await request.json(); } catch { return null; } };

  // 로그인 사용자
  let user = null;
  const auth = request.headers.get('Authorization') || '';
  if (auth.startsWith('Bearer ')) {
    const th = await sha256(auth.slice(7));
    user = await DB.prepare('select u.id, u.nickname from sessions s join users u on u.id=s.user_id where s.token_hash=?').bind(th).first();
    if (user) await DB.prepare('update sessions set last_seen=datetime("now") where token_hash=?').bind(th).run();
  }

  try {
    // ================= 인증 =================
    if (path === '/auth/register' && m === 'POST') {
      const b = await body() || {};
      const nick = String(b.nickname || '').trim(), pin = String(b.pin || '');
      if (nick.length < 2 || nick.length > 20) return bad('닉네임은 2~20자');
      if (!/^\d{4,6}$/.test(pin)) return bad('PIN은 숫자 4~6자리');
      if (await DB.prepare('select 1 from users where nickname=?').bind(nick).first()) return bad('이미 있는 닉네임입니다', 409);
      const id = uuid(), salt = randHex(16);
      await DB.prepare('insert into users(id,nickname,pin_hash,pin_salt) values(?,?,?,?)').bind(id, nick, await pinHash(pin, salt), salt).run();
      return J(await newSession(DB, id, nick));
    }
    if (path === '/auth/login' && m === 'POST') {
      const b = await body() || {};
      const nick = String(b.nickname || '').trim(), pin = String(b.pin || '');
      const u = await DB.prepare('select * from users where nickname=?').bind(nick).first();
      if (!u) return bad('닉네임 또는 PIN이 틀립니다', 401);
      if (u.locked_until && u.locked_until > new Date().toISOString()) return bad(`잠김 — ${LOCK_MIN}분 후 다시 시도`, 429);
      if (await pinHash(pin, u.pin_salt) !== u.pin_hash) {
        const f = (u.failed_count || 0) + 1;
        const lock = f >= LOCK_FAILS ? new Date(Date.now() + LOCK_MIN * 60000).toISOString() : null;
        await DB.prepare('update users set failed_count=?, locked_until=? where id=?').bind(lock ? 0 : f, lock, u.id).run();
        return bad(lock ? `${LOCK_FAILS}회 실패 — ${LOCK_MIN}분 잠김` : `닉네임 또는 PIN이 틀립니다 (${f}/${LOCK_FAILS})`, 401);
      }
      await DB.prepare('update users set failed_count=0, locked_until=null where id=?').bind(u.id).run();
      return J(await newSession(DB, u.id, u.nickname));
    }
    if (path === '/auth/me' && m === 'GET') return user ? J(user) : bad('login required', 401);
    if (path === '/auth/logout' && m === 'POST') {
      if (auth.startsWith('Bearer ')) await DB.prepare('delete from sessions where token_hash=?').bind(await sha256(auth.slice(7))).run();
      return J({ ok: true });
    }

    // ================= 코스 (읽기는 공개) =================
    if (path === '/courses' && m === 'GET') {
      const cs = (await DB.prepare('select * from courses order by club_name').all()).results;
      const hs = (await DB.prepare('select * from holes order by course_id, course_name, hole').all()).results;
      return J(cs.map(c => ({
        course_id: c.id, club_name: c.club_name, status: c.status, tee_order: JSON.parse(c.tee_order || '[]'),
        mine: !!user && c.created_by === user.id,
        courses: JSON.parse(c.course_names || '[]').map(n => ({
          name: n, holes: hs.filter(h => h.course_id === c.id && h.course_name === n)
            .map(h => ({ hole: h.hole, par: h.par, hdcp: h.hdcp, tees_m: JSON.parse(h.tees_m || '{}'), image_url: h.image_url, note: h.note }))
        }))
      })));
    }
    if (path === '/courses' && m === 'POST') {
      // 워커: 무엇이든 씀. 로그인 사용자(JSON 가져오기): 새 구장이거나 본인이 만든 구장만.
      if (!isWorker && !user) return bad('login required', 401);
      const c = await body(); if (!c?.course_id || !Array.isArray(c.courses)) return bad('course.json 형식 아님');
      if (!isWorker) {
        const ex = await DB.prepare('select created_by, status from courses where id=?').bind(c.course_id).first();
        if (ex && ex.created_by !== user.id) return bad('이미 등록된 구장은 덮어쓸 수 없습니다', 403);
      }
      const tees = c.tee_order?.length ? c.tee_order : [...new Set(c.courses.flatMap(x => x.holes.flatMap(h => Object.keys(h.tees_m || {}))))];
      const stmts = [DB.prepare('insert into courses(id,club_name,source_url,tee_order,course_names,status,created_by,updated_at) values(?1,?2,?3,?4,?5,"ready",?6,datetime("now")) on conflict(id) do update set club_name=?2,source_url=?3,tee_order=?4,course_names=?5,status="ready",updated_at=datetime("now")')
        .bind(c.course_id, c.club_name, c.source || null, JSON.stringify(tees), JSON.stringify(c.courses.map(x => x.name)), isWorker ? null : user.id),
        DB.prepare('delete from holes where course_id=?').bind(c.course_id)];
      for (const x of c.courses) for (const h of x.holes)
        stmts.push(DB.prepare('insert into holes(course_id,course_name,hole,par,hdcp,tees_m,image_url,note) values(?,?,?,?,?,?,?,?)')
          .bind(c.course_id, x.name, +h.hole, +h.par, h.hdcp ?? null, JSON.stringify(h.tees_m || {}), h.image_url || null, h.note || null));
      await DB.batch(stmts);
      return J({ ok: true, holes: stmts.length - 2 });
    }

    const cd = path.match(/^\/courses\/([\w-]+)$/);
    if (cd && m === 'DELETE') {
      // 워커: 무엇이든. 로그인 사용자: 본인이 등록한 구장, 또는 홀 데이터가 없는 미완성(수집중·실패) 자리표시
      if (!isWorker && !user) return bad('login required', 401);
      const c = await DB.prepare('select c.*, (select count(*) from holes h where h.course_id=c.id) n from courses c where c.id=?').bind(cd[1]).first();
      if (!c) return bad('not found', 404);
      const can = isWorker || c.created_by === user.id || (c.n === 0 && c.status !== 'ready');
      if (!can) return bad('수집이 끝난 공용 구장은 삭제할 수 없습니다', 403);
      await DB.batch([DB.prepare('delete from holes where course_id=?').bind(c.id), DB.prepare('delete from courses where id=?').bind(c.id),
        DB.prepare('update crawl_requests set status="canceled", finished_at=datetime("now") where course_id=? and status in ("searching","confirm","queued","running")').bind(c.id)]);
      return J({ ok: true });
    }

    // ================= 코스맵 이미지 (R2) =================
    if (path.startsWith('/images/')) {
      const key = decodeURIComponent(path.slice(8));
      if (m === 'PUT') {
        if (!isWorker) return bad('unauthorized', 401);
        await env.MAPS.put(key, request.body, { httpMetadata: { contentType: request.headers.get('content-type') || 'image/jpeg' } });
        return J({ ok: true, url: '/api/images/' + key });
      }
      const obj = await env.MAPS.get(key); if (!obj) return bad('not found', 404);
      return new Response(obj.body, { headers: { 'content-type': obj.httpMetadata?.contentType || 'image/jpeg', 'cache-control': 'public, max-age=2592000' } });
    }

    // ================= 구장추가 큐 =================
    if (path === '/crawl-requests' && m === 'GET') {
      const q = new URL(request.url).searchParams.get('status');
      if (isWorker && q) return J((await DB.prepare('select * from crawl_requests where status=? order by created_at limit 5').bind(q).all()).results);
      if (!user) return bad('login required', 401);
      return J((await DB.prepare('select * from crawl_requests where user_id=? order by created_at desc limit 10').bind(user.id).all()).results);
    }
    if (path === '/crawl-requests' && m === 'POST') {
      if (!user) return bad('login required', 401);
      const b = await body() || {};
      const name = String(b.club_name || '').trim(), url = String(b.url || '').trim();
      if (!name) return bad('골프장명 필요');
      if (url && !/^https?:\/\//.test(url)) return bad('주소는 http(s)://로 시작');
      const id = uuid();
      // 주소가 있으면 바로 수집 대기, 없으면 워커가 먼저 홈페이지를 검색
      await DB.prepare('insert into crawl_requests(id,user_id,club_name,url,status) values(?,?,?,?,?)').bind(id, user.id, name, url, url ? 'queued' : 'searching').run();
      // 주소가 있으면 바로 수집기(GitHub) 호출. 이름만이면 앱이 이어서 /search 로 Cloudflare에서 즉시 검색한다
      // (Anthropic 키가 없을 때만 GitHub 수집기가 검색까지 맡음)
      if (url || !env.ANTHROPIC_API_KEY) wake(env, waitUntil);
      return J({ id, search_here: !url && !!env.ANTHROPIC_API_KEY });
    }
    const crs = path.match(/^\/crawl-requests\/([\w-]+)\/search$/);
    if (crs && m === 'POST') {                             // 이름 → 공식 홈페이지 (Cloudflare에서 즉시)
      if (!user) return bad('login required', 401);
      const r = await DB.prepare('select * from crawl_requests where id=? and user_id=?').bind(crs[1], user.id).first();
      if (!r) return bad('not found', 404);
      if (r.status !== 'searching') return J(r);
      if (!env.ANTHROPIC_API_KEY) { wake(env, waitUntil); return bad('검색 키 미설정 — 수집기가 대신 찾습니다', 400); }
      let res;
      try { res = await findHomepage(env, r.club_name); }
      catch (e) { wake(env, waitUntil); return bad('검색 실패 — 수집기가 대신 찾습니다: ' + String(e.message || e).slice(0, 120), 502); }
      await DB.prepare('update crawl_requests set status="confirm", candidate_url=?, message=? where id=?').bind(res.url, res.note, r.id).run();
      return J({ ...r, status: 'confirm', candidate_url: res.url, message: res.note });
    }
    const crd = path.match(/^\/crawl-requests\/([\w-]+)$/);
    if (crd && m === 'DELETE') {                           // 사용자: 요청 현황에서 지우기
      if (!user) return bad('login required', 401);
      await DB.prepare('delete from crawl_requests where id=? and user_id=?').bind(crd[1], user.id).run();
      return J({ ok: true });
    }
    const cr = path.match(/^\/crawl-requests\/([\w-]+)(?:\/(confirm|cancel))?$/);
    if (cr && cr[2] && m === 'POST') {                     // 사용자: 찾은 주소 확인/취소
      if (!user) return bad('login required', 401);
      const r = await DB.prepare('select * from crawl_requests where id=? and user_id=?').bind(cr[1], user.id).first();
      if (!r) return bad('not found', 404);
      if (cr[2] === 'cancel') { await DB.prepare('update crawl_requests set status="canceled", finished_at=datetime("now") where id=?').bind(r.id).run(); return J({ ok: true }); }
      const b = await body() || {};
      const url = String(b.url || r.candidate_url || '').trim();
      if (!/^https?:\/\//.test(url)) return bad('확인할 주소가 없습니다');
      await DB.prepare('update crawl_requests set url=?, status="queued", message=null where id=?').bind(url, r.id).run();
      wake(env, waitUntil);
      return J({ ok: true });
    }
    if (cr && !cr[2] && m === 'PATCH') {                   // 워커: 상태 갱신
      if (!isWorker) return bad('unauthorized', 401);
      const b = await body() || {};
      await DB.prepare('update crawl_requests set status=coalesce(?,status), course_id=coalesce(?,course_id), message=coalesce(?,message), candidate_url=coalesce(?,candidate_url), finished_at=case when ? in ("done","failed") then datetime("now") else finished_at end where id=?')
        .bind(b.status ?? null, b.course_id ?? null, b.message ?? null, b.candidate_url ?? null, b.status ?? null, cr[1]).run();
      if (b.status === 'running' && b.course_id) {
        const r = await DB.prepare('select club_name, url from crawl_requests where id=?').bind(cr[1]).first();
        await DB.prepare('insert or ignore into courses(id,club_name,source_url,status) values(?,?,?,"pending")').bind(b.course_id, r?.club_name || b.course_id, r?.url || null).run();
      }
      if (b.status === 'failed' && b.course_id) await DB.prepare('update courses set status="failed" where id=? and status="pending"').bind(b.course_id).run();
      return J({ ok: true });
    }

    // ================= 수집기 상태 =================
    if (path === '/worker/ping' && m === 'POST') {
      if (!isWorker) return bad('unauthorized', 401);
      const now = new Date().toISOString();
      const phase = new URL(request.url).searchParams.get('phase');   // preparing | working | idle
      const up = (k, v) => DB.prepare('insert into worker_status(k,v,updated_at) values(?,?,datetime("now")) on conflict(k) do update set v=excluded.v, updated_at=excluded.updated_at').bind(k, v);
      await DB.batch([up('last_seen', now), ...(phase ? [up('phase', `${phase} ${now}`)] : [])]);
      return J({ ok: true });
    }
    if (path === '/worker/status' && m === 'GET') {
      const rows = (await DB.prepare('select k, v from worker_status').all()).results;
      const kv = Object.fromEntries(rows.map(r => [r.k, r.v]));
      return J({ last_seen: kv.last_seen || null, auto: !!(env.GH_TOKEN && env.GH_REPO), last_wake: kv.last_wake || null, search_here: !!env.ANTHROPIC_API_KEY, phase: kv.phase || null });
    }
    if (path === '/worker/wake' && m === 'POST') {           // 앱의 '지금 실행' 버튼
      if (!user && !isWorker) return bad('login required', 401);
      const r = await wake(env, null);
      return r.ok ? J({ ok: true }) : bad(r.msg || '자동 실행이 설정되지 않았습니다', r.status || 400);
    }

    // ================= 라운드 (로그인 필요) =================
    if (!user) return bad('login required', 401);
    const own = async id => !!(await DB.prepare('select 1 from rounds where id=? and user_id=?').bind(id, user.id).first());

    if (path === '/rounds' && m === 'POST') {
      const r = await body(); if (!r?.id) return bad('id 필요');
      const ex = await DB.prepare('select user_id from rounds where id=?').bind(r.id).first();
      if (ex && ex.user_id !== user.id) return bad('forbidden', 403);
      // device_id: v1 운영 DB에서 NOT NULL 컬럼이라 사용자 id를 채워 둔다
      await DB.prepare('insert or replace into rounds(id,device_id,user_id,course_id,club_name,played_on,tee,front,back,finished_at) values(?,?,?,?,?,?,?,?,?,(select finished_at from rounds where id=?))')
        .bind(r.id, user.id, user.id, r.course_id, r.club_name, r.played_on, r.tee, r.front, r.back || null, r.id).run();
      return J({ ok: true });
    }
    const putHole = (rid, h) => DB.prepare('insert or replace into hole_scores(round_id,seq,course_name,hole,par,tee,length_m,remain_m,score,putts,fairway,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,datetime("now"))')
      .bind(rid, +h.seq, h.course_name, +h.hole, +h.par, h.tee, h.length_m ?? null, +h.par === 3 ? null : (h.remain_m ?? null), h.score ?? null, h.putts ?? null, h.fairway ?? null);
    const rh1 = path.match(/^\/rounds\/([\w-]+)\/holes\/(\d+)$/);
    if (rh1 && m === 'PUT') {
      if (!(await own(rh1[1]))) return bad('round not found', 404);
      const h = await body(); if (!h) return bad('body 필요');
      await putHole(rh1[1], { ...h, seq: +rh1[2] }).run();
      return J({ ok: true });
    }
    const rh = path.match(/^\/rounds\/([\w-]+)\/holes$/);
    if (rh && m === 'PUT') {
      if (!(await own(rh[1]))) return bad('round not found', 404);
      const hs = await body(); if (!Array.isArray(hs)) return bad('array 필요');
      await DB.batch(hs.map(h => putHole(rh[1], h)));
      return J({ ok: true, n: hs.length });
    }
    const rf = path.match(/^\/rounds\/([\w-]+)\/finish$/);
    if (rf && m === 'POST') {
      if (!(await own(rf[1]))) return bad('round not found', 404);
      await DB.prepare('update rounds set finished_at=datetime("now") where id=?').bind(rf[1]).run();
      return J({ ok: true });
    }
    const rd = path.match(/^\/rounds\/([\w-]+)$/);
    if (rd && m === 'DELETE') {
      if (!(await own(rd[1]))) return bad('round not found', 404);
      await DB.batch([DB.prepare('delete from hole_scores where round_id=?').bind(rd[1]), DB.prepare('delete from rounds where id=?').bind(rd[1])]);
      return J({ ok: true });
    }
    if ((path === '/rounds' || path === '/rounds/active') && m === 'GET') {
      const active = path === '/rounds/active';
      const rs = (await DB.prepare(`select * from rounds where user_id=? ${active ? 'and finished_at is null' : ''} order by played_on desc, created_at desc limit ${active ? 1 : 50}`).bind(user.id).all()).results;
      if (!rs.length) return J(active ? null : []);
      const ids = rs.map(r => r.id);
      const ss = (await DB.prepare(`select * from hole_scores where round_id in (${ids.map(() => '?').join(',')}) order by round_id, seq`).bind(...ids).all()).results;
      const out = rs.map(r => ({ ...r, holes: ss.filter(s => s.round_id === r.id) }));
      return J(active ? out[0] : out);
    }
    if (path === '/stats' && m === 'GET') {
      const drive = (await DB.prepare('select s.tee, count(s.drive_m) n, round(avg(s.drive_m)) avg_m, max(s.drive_m) max_m from hole_scores s join rounds r on r.id=s.round_id where r.user_id=? and s.drive_m is not null group by s.tee').bind(user.id).all()).results;
      const sc = await DB.prepare('select count(*) rounds, round(avg(t),1) avg_score, min(t) best from (select r.id, sum(s.score) t, count(s.score) n from rounds r join hole_scores s on s.round_id=r.id where r.user_id=? and r.finished_at is not null group by r.id having n=18)').bind(user.id).first();
      const putt = await DB.prepare('select round(avg(s.putts),2) avg_putts from hole_scores s join rounds r on r.id=s.round_id where r.user_id=? and s.putts is not null').bind(user.id).first();
      const fw = await DB.prepare('select count(*) n, sum(case when fairway="hit" then 1 else 0 end) hit from hole_scores s join rounds r on r.id=s.round_id where r.user_id=? and s.fairway is not null').bind(user.id).first();
      return J({ drive, score: sc, putts: putt?.avg_putts ?? null, fairway: fw?.n ? Math.round(fw.hit / fw.n * 100) : null });
    }
    return bad('not found', 404);
  } catch (e) {
    return bad(String(e.message || e), 500);
  }
}

// ---------- 이름 → 공식 홈페이지 (Anthropic 웹 검색) ----------
const NOT_OFFICIAL = /(kimcaddie|dbegl|teescanner|tigerbooking|xgolf|smartscore|naver\.com|daum\.net|kakao|tistory|blog|cafe|instagram|facebook|youtube|wikipedia|namu\.wiki|ktriptips|golfpass|teetimegolfpass)/i;
const hostOf = u => { try { return new URL(u).hostname.toLowerCase().replace(/^www\./, ''); } catch { return ''; } };

async function askClaude(env, model, prompt) {
  const r = await fetch((env.ANTHROPIC_BASE_URL || 'https://api.anthropic.com') + '/v1/messages', {
    method: 'POST',
    headers: { 'x-api-key': env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
    body: JSON.stringify({
      model, max_tokens: 2000,
      tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 3, user_location: { type: 'approximate', country: 'KR' } }],
      messages: [{ role: 'user', content: prompt }],
    }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`Anthropic ${r.status} ${j?.error?.message || ''}`.trim());
  return j;
}

async function searchOnce(env, name, dead) {
  const prompt = `대한민국 골프장 '${name}'의 **공식 홈페이지** 주소를 웹 검색으로 찾아라.
- 예약·중개·리뷰·블로그·지도·위키 사이트는 공식 홈페이지가 아니다.
- 운영사 통합 사이트(예: 골프존카운티)라면 해당 골프장 전용 페이지 주소를 택한다.
- 같은 이름의 해외 골프장과 혼동하지 마라. 주소(시·군)로 국내 골프장인지 확인하라.
- 인수·리뉴얼로 도메인이 바뀌었을 수 있으니 최신 정보를 우선하라.
- 가능하면 '코스소개/코스안내' 페이지 주소를, 못 찾으면 메인 주소를 답한다.${dead.length ? `\n- 다음 주소는 접속이 안 되는 옛 주소이니 제외하라: ${dead.join(', ')}` : ''}
마지막 줄에 JSON만: {"url": "https://..." 또는 null, "location": "시·군", "confidence": 0~1, "reason": "한 줄"}`;
  let j;
  const fast = env.SEARCH_MODEL || 'claude-haiku-5-5', strong = env.SEARCH_MODEL_FALLBACK || 'claude-sonnet-5-5';
  try { j = await askClaude(env, fast, prompt); }
  catch (e) { j = await askClaude(env, strong, prompt); }           // 빠른 모델이 실패하면 기존 모델로
  const seen = new Set();
  for (const b of j.content || []) if (b.type === 'web_search_tool_result' && Array.isArray(b.content)) b.content.forEach(x => seen.add(hostOf(x.url)));
  const text = (j.content || []).filter(b => b.type === 'text').map(b => b.text).join('');
  const all = text.match(/\{[^{}]*"url"[^{}]*\}/gs);
  if (!all) return { url: null, note: '검색 결과를 해석하지 못했습니다' };
  const o = JSON.parse(all[all.length - 1]);
  if (!o.url || !/^https?:\/\//.test(o.url)) return { url: null, note: o.reason || '공식 홈페이지를 찾지 못했습니다' };
  const h = hostOf(o.url);
  if (NOT_OFFICIAL.test(h)) return { url: null, note: `공식 홈페이지가 아닌 사이트만 찾았습니다(${h}). 주소를 직접 입력해 주세요` };
  let note = [o.location, o.confidence != null ? `신뢰도 ${o.confidence}` : '', o.reason].filter(Boolean).join(' · ');
  if (!seen.has(h)) note += ' · ⚠ 검색 결과에 직접 나온 주소가 아님, 꼭 확인';
  return { url: o.url, note };
}

async function reachable(url) {
  try {
    const r = await fetch(url, { redirect: 'follow', headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) });
    return { ok: r.status < 500, url: r.url || url };
  } catch { return { ok: false, url }; }
}

async function findHomepage(env, name) {
  const dead = [];
  for (let i = 0; i < 2; i++) {
    const r = await searchOnce(env, name, dead);
    if (!r.url) return r;
    const c = await reachable(r.url);
    if (c.ok) return { url: c.url, note: r.note };
    dead.push(r.url);
  }
  return { url: null, note: `찾은 주소가 접속되지 않습니다(${dead.join(', ')}). 주소를 직접 입력해 주세요` };
}

// GitHub Actions 수집기 깨우기 (GH_TOKEN·GH_REPO 시크릿이 있을 때만)
async function wake(env, waitUntil) {
  if (!env.GH_TOKEN || !env.GH_REPO) return { ok: false, msg: '자동 실행 미설정(GH_TOKEN/GH_REPO)' };
  const run = (async () => {
    let res, msg;
    try {
      res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${env.GH_TOKEN}`, Accept: 'application/vnd.github+json', 'User-Agent': 'golf-yardage', 'X-GitHub-Api-Version': '2022-11-28', 'content-type': 'application/json' },
        body: JSON.stringify({ event_type: 'crawl' }),
      });
      msg = res.status === 204 ? 'ok' : `GitHub ${res.status} ${(await res.text()).slice(0, 120)}`;
    } catch (e) { msg = 'GitHub 호출 실패 ' + e.message; }
    try {
      await env.DB.prepare('insert into worker_status(k,v,updated_at) values("last_wake",?,datetime("now")) on conflict(k) do update set v=excluded.v, updated_at=excluded.updated_at')
        .bind(`${new Date().toISOString()} ${msg}`).run();
    } catch {}
    return { ok: msg === 'ok', msg, status: res?.status === 204 ? 200 : 502 };
  })();
  if (waitUntil) { waitUntil(run); return { ok: true }; }
  return run;
}

async function newSession(DB, userId, nickname) {
  const token = randHex(32);
  await DB.prepare('insert into sessions(token_hash,user_id) values(?,?)').bind(await sha256(token), userId).run();
  return { token, user: { id: userId, nickname } };
}
