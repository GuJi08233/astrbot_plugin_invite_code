/**
 * AstrBot 邀请链接验证 API
 *
 * 部署到 Cloudflare Worker，使用 Browser Run 检查页面全文内容。
 * 需要添加 Browser 绑定，变量名: BROWSER
 */

import puppeteer from "@cloudflare/puppeteer";

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }

    if (request.method !== "POST") {
      return Response.json(
        { error: "Only POST allowed" },
        { status: 405, headers: corsHeaders() }
      );
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json(
        { error: "Body must be JSON" },
        { status: 400, headers: corsHeaders() }
      );
    }

    const { url, valid_text, error_texts, description, token } = body;

    // 鉴权
    if (env.AUTH_TOKEN && token !== env.AUTH_TOKEN) {
      return Response.json(
        { error: "Unauthorized" },
        { status: 401, headers: corsHeaders() }
      );
    }

    if (!url) {
      return Response.json(
        { error: "Missing url" },
        { status: 400, headers: corsHeaders() }
      );
    }

    try {
      new URL(url);
    } catch {
      return Response.json(
        { error: "Invalid URL" },
        { status: 400, headers: corsHeaders() }
      );
    }

    // valid_text: string | string[]
    const validTexts = Array.isArray(valid_text) ? valid_text : [valid_text];
    const errorTexts = Array.isArray(error_texts) ? error_texts : [];

    if (!validTexts.filter(Boolean).length && !errorTexts.length) {
      return Response.json(
        { valid: true, message: "No rule, skip verify", title: "" },
        { headers: corsHeaders() }
      );
    }

    if (!env.BROWSER) {
      return Response.json(
        { valid: true, message: "Browser binding not found", title: "", status: 0 },
        { headers: corsHeaders() }
      );
    }

    try {
      const browser = await puppeteer.launch(env.BROWSER);
      const page = await browser.newPage();

      let title = "";
      let bodyText = "";
      let status = 0;
      let finalUrl = "";

      try {
        const resp = await page.goto(url, {
          waitUntil: "load",
          timeout: 20000,
        });
        title = await page.title();
        finalUrl = page.url();
        const html = await page.content();
        bodyText = html
          .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
          .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
          .replace(/<[^>]+>/g, " ")
          .replace(/&nbsp;/g, " ")
          .replace(/\s+/g, " ")
          .trim();
        status = resp ? resp.status() : 0;
      } finally {
        await page.close();
        await browser.close();
      }

      const content = title + " " + bodyText;
      const desc = description || "Link";

      console.log(`[verify] url=${url}`);
      console.log(`[verify] finalUrl=${finalUrl}`);
      console.log(`[verify] status=${status} title=${title}`);

      // Check valid indicators
      for (const vt of validTexts) {
        if (vt && content.includes(vt)) {
          return Response.json(
            { valid: true, message: `${desc} - valid`, title, status },
            { headers: corsHeaders() }
          );
        }
      }

      // Check error indicators
      for (const err of errorTexts) {
        if (content.includes(err)) {
          return Response.json(
            {
              valid: false,
              message: `page contains "${err}"`,
              title,
              status,
            },
            { headers: corsHeaders() }
          );
        }
      }

      // No match
      return Response.json(
        {
          valid: false,
          message: "No expected content found on page",
          title,
          status,
          finalUrl,
          bodyPreview: (bodyText || "").substring(0, 500),
        },
        { headers: corsHeaders() }
      );
    } catch (err) {
      console.error(`Verify error: ${url}`, err);
      return Response.json(
        {
          valid: true,
          message: `Error, treat as valid: ${err.message}`,
          title: "",
          status: 0,
        },
        { headers: corsHeaders() }
      );
    }
  },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}
