-- Cloudflare D1 (SQLite) — 신규 설치용 전체 스키마
-- 적용: npx wrangler d1 execute golf-yardage --remote --file=cf/schema.sql   (로컬 테스트는 --local)
-- 기존 설치(v1) 업그레이드는 deploy_token.sh 가 cf/migrate_002.sql 의 ALTER 문을 하나씩 실행한다.

create table if not exists users (
  id text primary key,
  nickname text not null unique collate nocase,
  pin_hash text not null,            -- PBKDF2-SHA256 hex
  pin_salt text not null,
  failed_count integer default 0,
  locked_until text,
  created_at text default (datetime('now'))
);

create table if not exists sessions (
  token_hash text primary key,       -- sha256(token)
  user_id text not null,
  created_at text default (datetime('now')),
  last_seen text default (datetime('now'))
);

create table if not exists courses (
  id text primary key,
  club_name text not null,
  source_url text,
  tee_order text default '[]',
  course_names text default '[]',
  status text default 'ready',       -- ready | pending | failed
  created_by text,                   -- users.id (워커가 만든 건 null)
  updated_at text default (datetime('now'))
);

create table if not exists holes (
  course_id text not null,
  course_name text not null,
  hole integer not null,
  par integer not null,
  hdcp integer,
  tees_m text not null default '{}',
  greens_m text,                     -- JSON {"우":{"BLACK":404},"좌":{"BLACK":435}} — 그린이 둘인 홀만
  image_url text,
  note text,
  primary key (course_id, course_name, hole)
);

create table if not exists rounds (
  id text primary key,
  device_id text,                    -- v1 호환
  user_id text,
  course_id text,
  club_name text,
  played_on text not null,
  tee text not null,
  front text not null,
  back text,
  finished_at text,
  created_at text default (datetime('now'))
);

create table if not exists hole_scores (
  round_id text not null,
  seq integer not null,
  course_name text not null,
  hole integer not null,
  par integer not null,
  tee text not null,
  length_m integer,
  remain_m integer,
  drive_m integer generated always as (case when par >= 4 and remain_m is not null and length_m is not null then length_m - remain_m end) stored,
  score integer,
  putts integer,
  fairway text,
  green text,                        -- 그린이 둘인 홀에서 고른 그린(우/좌)
  updated_at text default (datetime('now')),
  primary key (round_id, seq)
);

create table if not exists crawl_requests (
  id text primary key,
  device_id text,
  user_id text,
  club_name text not null,
  url text not null default '',      -- 비어 있으면 워커가 검색
  candidate_url text,                -- 워커가 찾은 홈페이지 (사용자 확인 대기)
  status text default 'queued',      -- searching | confirm | queued | running | done | failed | canceled
  course_id text,
  message text,
  created_at text default (datetime('now')),
  finished_at text
);

create table if not exists worker_status (   -- 수집기 마지막 실행 시각, GitHub 호출 결과
  k text primary key,
  v text,
  updated_at text default (datetime('now'))
);
