-- v1 → v2 업그레이드. 각 줄을 따로 실행(이미 있는 컬럼이면 실패해도 무시).
-- deploy_token.sh 가 한 줄씩 --command 로 실행한다.
ALTER TABLE rounds ADD COLUMN user_id text;
ALTER TABLE rounds ADD COLUMN finished_at text;
ALTER TABLE courses ADD COLUMN created_by text;
ALTER TABLE crawl_requests ADD COLUMN user_id text;
ALTER TABLE crawl_requests ADD COLUMN candidate_url text;
CREATE INDEX IF NOT EXISTS idx_rounds_user ON rounds(user_id, played_on);
