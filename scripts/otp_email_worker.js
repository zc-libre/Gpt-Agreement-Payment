// Cloudflare Email Worker — receives mail via Email Routing catch-all,
// extracts a 6-digit OTP, stores it in KV keyed by recipient address.
//
// Bindings (set by setup_cf_email_worker.py):
//   OTP_KV       — KV namespace for {recipient → {otp, ts, from, subject}}
//   FALLBACK_TO  — (optional) plain_text. If set, forward raw email to this
//                  address as well (useful during migration off IMAP/QQ).
//
// Pipeline reads KV via CF API (CTF-reg/cf_kv_otp_provider.py).

export default {
  async email(message, env, ctx) {
    const to = (message.to || '').toLowerCase();
    const from = message.from || '';

    // Read the raw RFC822 message into a string
    let raw = '';
    try {
      const reader = message.raw.getReader();
      const decoder = new TextDecoder('utf-8', { fatal: false });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        raw += decoder.decode(value, { stream: true });
      }
      raw += decoder.decode();
    } catch (e) {
      console.error('raw read failed:', e && e.message);
    }

    // Pull the Subject header out for fast-path matching (most OpenAI OTP
    // mails put the code right in the subject)
    const subjMatch = raw.match(/^Subject:\s*(.+?)(?:\r?\n[^\s])/ms);
    const subject = subjMatch ? subjMatch[1].trim().slice(0, 200) : '';

    // 收件地址 + 发件地址里的数字（zone 名常含 6 位，会被 fallback regex
    // 误抽成 OTP，比如 random@123456.example.com 这种 zone → "123456" 假阳性）
    const addrDigits = ((to + ' ' + from).match(/\d/g) || []).join('');
    const isFromAddr = (s) => addrDigits.length >= 6 && addrDigits.includes(s);

    // ── 邮件正文预处理 ──
    // OpenAI 的 OTP 邮件是 quoted-printable + HTML，原始 raw 里：
    //   - "verification code: 941275" 在 HTML 模板里被 <td>/MSO 注释隔开几百字符，
    //     keyword regex 限 40 字符匹不中
    //   - CSS 里的 color:#353740 之类的 hex 颜色会被纯数字 fallback 误抽
    //   - quoted-printable soft-break (=\n) 可能把数字打散
    // 解决：先 strip header → decode QP → 去 <style> + HTML 标签 → 在干净文本上 regex
    const headerEnd = raw.search(/\r?\n\r?\n/);
    const bodyRaw = headerEnd >= 0 ? raw.slice(headerEnd) : raw;
    const bodyDecoded = bodyRaw
      .replace(/=\r?\n/g, '')                                                  // QP soft line breaks
      .replace(/=([0-9A-Fa-f]{2})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
    const bodyText = bodyDecoded
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')                               // 整段 <style> 干掉，免得 CSS 颜色被抽
      .replace(/<!--[\s\S]*?-->/g, ' ')                                        // HTML 注释（含 MSO conditional）
      .replace(/<[^>]+>/g, ' ')                                                // 所有标签
      .replace(/&(nbsp|amp|lt|gt|quot|#39);/g, ' ')
      .replace(/#[0-9A-Fa-f]{6}\b/g, ' ')                                      // 兜底再清一次孤立 hex 颜色
      .replace(/\s+/g, ' ')
      .trim();
    const haystack = subject + ' ' + bodyText;

    // OTP extraction — semantic context first to avoid grabbing tracking ids,
    // 排除 # 前缀（CSS 颜色），排除地址中已有的数字。
    let otp = null;
    const candidates = [
      // 最强：OpenAI / ChatGPT 邮件的标准措辞
      /verification code\s*(?:to continue|is)?[:\s]+(\d{6})\b/i,
      /\bcode\s*(?:is|to continue)?[:\s]+(\d{6})\b/i,
      // 一般：keyword 附近 80 字符内（比原来 40 更宽，HTML 内文）
      /(?:verification|one[-\s]*time|verify|验证码)[^\d]{0,80}(\d{6})\b/i,
      /\b(?:chatgpt|openai)\b[^\d]{0,80}(\d{6})\b/i,
      // 兜底：纯文本里任意独立 6 位数字（CSS 颜色已在预处理时 strip）
      /(?<![#&\w])\b(\d{6})\b/,
    ];
    for (const re of candidates) {
      const m = re.exec(haystack);
      if (m && !isFromAddr(m[1])) { otp = m[1]; break; }
    }

    // Diagnostic: 不论是否抽到 OTP，都把 raw RFC822 body 存一份（key: <to>:raw）
    // 用来诊断 regex 是否抽对。pipeline 拉 OTP 时只读 <to> key，不会读这个。
    if (to && raw) {
      try {
        await env.OTP_KV.put(`${to}:raw`, raw, { expirationTtl: 600 });
      } catch (e) {
        console.error('raw put failed:', e && e.message);
      }
    }

    if (otp && to) {
      const payload = JSON.stringify({
        otp,
        ts: Date.now(),
        from,
        subject,
      });
      try {
        await env.OTP_KV.put(to, payload, { expirationTtl: 600 });
        console.log(`stored OTP for ${to.slice(0, 40)} (subject="${subject.slice(0, 60)}")`);
      } catch (e) {
        console.error('KV put failed:', e && e.message);
      }
    } else {
      console.log(`no OTP extracted to=${to.slice(0, 40)} subject="${subject.slice(0, 60)}"`);
    }

    // Optional: forward raw email to fallback mailbox (e.g. existing QQ inbox)
    // Useful during the IMAP→KV migration to keep both paths warm.
    if (env.FALLBACK_TO) {
      try {
        await message.forward(env.FALLBACK_TO);
      } catch (e) {
        console.error('forward failed:', e && e.message);
      }
    }
  },
};
