/**
 * Cloudflare Email Worker for support@bondrova.com
 *
 * For every email that reaches support@:
 *   1. forwards it to your personal inbox (FORWARD_TO), and
 *   2. posts the raw email to Bondrova so it appears in Admin → Support
 *      (new ticket, or added to an existing ticket when the subject has [BDR-XXXXXX]).
 *
 * Settings → Variables (in the Worker):
 *   FORWARD_TO      your Gmail address (must be a verified Email Routing destination)
 *   INBOUND_URL     https://api.bondrova.com/api/v1/support/inbound
 *   INBOUND_SECRET  (type: Secret) the same value as SUPPORT_INBOUND_SECRET on the Railway backend
 */
export default {
  async email(message, env, ctx) {
    // Read the raw message once so we can both forward and archive it.
    const raw = await new Response(message.raw).arrayBuffer();

    // 1) Always deliver to the personal inbox first — if Bondrova is down you still get the email.
    if (env.FORWARD_TO) {
      try {
        await message.forward(env.FORWARD_TO);
      } catch (e) {
        console.log("forward failed", e && e.message);
      }
    }

    // 2) Copy into the Bondrova admin inbox (skip anything over 10 MB).
    if (env.INBOUND_URL && env.INBOUND_SECRET && raw.byteLength <= 10 * 1024 * 1024) {
      ctx.waitUntil(
        fetch(env.INBOUND_URL, {
          method: "POST",
          headers: { "Content-Type": "message/rfc822", "X-Inbound-Secret": env.INBOUND_SECRET },
          body: raw,
        })
          .then((r) => console.log("bondrova inbound", r.status))
          .catch((e) => console.log("bondrova inbound failed", e && e.message)),
      );
    }
  },
};
