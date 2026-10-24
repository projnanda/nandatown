This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Tests

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run typecheck
```

The tests exercise the real API handler, form action and rendered catalog page
with external persistence and request context replaced by test doubles.
Unexpected network access fails the test. They require no database,
credentials or live endpoint, and they do not prove database transactions,
browser hydration or a deployed Next server.

## Catalog boundary

`POST /api/skills` accepts JSON up to 256 KiB and uses the same field
validation as the HTML form. Source URLs are validated as HTTP(S) syntax only.
Submission never fetches the submitted link: new URL and GitHub entries are
stored with `reachable: null` and the page labels them "link not checked". A
saved catalog record is not evidence that a link is live or safe, an
endorsement, or permission to execute a submitted skill. A catalog insert and
its audit-history row commit in one database transaction.

Maximum field lengths are 200 characters for `name` and `author`, 254 for
`email`, 39 for `github_username`, 2,000 for `description`, 2,048 for
`source_url`, 200,000 for inline `content`, 8,192 for `endpoints`, and 512 for
`tags`. Every field is trimmed, and `email` and `github_username` are checked
the way the form checks them. The request bound applies first, so inline
content made of multi-byte characters can be refused by the API below its
character limit. These bound new records, not aggregate catalog growth;
`GET /api/skills` still returns the complete public catalog, so a production
deployment still needs gateway-wide body and rate controls.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
