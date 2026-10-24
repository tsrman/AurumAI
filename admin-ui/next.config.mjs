import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Routes retired by the admin redesign keep working for bookmarks and
  // older links from the chat app.
  async redirects() {
    return [
      { source: "/admin", destination: "/admin/users", permanent: false },
      { source: "/admin/access", destination: "/admin/models", permanent: false },
      { source: "/agents/constructor", destination: "/admin/agents/new", permanent: false },
    ];
  },
};

export default withNextIntl(nextConfig);
