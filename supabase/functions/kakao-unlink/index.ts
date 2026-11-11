// 카카오 "연결 해제 웹훅" 받는 곳 (Supabase Edge Function, JWT 검증 끔).
// 사용자가 카카오 쪽에서 이 앱 연결을 끊으면 카카오가 user_id를 보냄 → unlinked 테이블에 쌓아 두면
// 앱(store.pop_unlinked)이 다음 접속 때 그 사람의 프로필·모집 참여·식사 기록을 지움.
// 진짜 카카오 요청인지는 Authorization: KakaoAK {어드민 키}로 확인 (함수 비밀값 KAKAO_ADMIN_KEY).
import { createClient } from "jsr:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  const key = Deno.env.get("KAKAO_ADMIN_KEY");
  if (!key || req.headers.get("authorization") !== `KakaoAK ${key}`) {
    return new Response("forbidden", { status: 401 });
  }
  const params = req.method === "POST"
    ? new URLSearchParams(await req.text())
    : new URL(req.url).searchParams;
  const id = params.get("user_id") ?? "";
  if (!/^\d{1,20}$/.test(id)) return new Response("bad request", { status: 400 });
  const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { error } = await db.from("unlinked").upsert({ uid: `k${id}` });
  return new Response(error ? "error" : "ok", { status: error ? 500 : 200 });
});
