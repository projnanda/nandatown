This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## GitHub PR gallery

The homepage and `/prgallery` count every repository PR, including closed,
unmerged PRs. Layer labels such as `layer:negotiation` override automatic
classification. Explicit layer declarations in a PR title or body take priority
over keyword guesses.

The existing GitHub webhook sends `pull_request` events to
`https://nandatown.projectnanda.org/api/github/webhook`. Set the same random
`GITHUB_WEBHOOK_SECRET` in its GitHub configuration and the Railway service
variables, then deploy. An unsigned request returns 401; a signed GitHub ping
returns 200. Keep the secret out of source control.

Railway runs one Node replica. Its shared process cache retains the last complete
snapshot if GitHub fails and has no Next.js 2 MB entry limit. Webhooks mark that
snapshot dirty; the next eligible page request refreshes it. Without an API token,
refreshes are at least five minutes apart and slow further as pagination grows
to reserve GitHub's anonymous request budget. A ten-minute refresh also catches
missed events. An optional `GITHUB_TOKEN` with read-only pull-request access allows
faster webhook updates. If adding replicas, move the snapshot and invalidation
state to a shared store first.

Run `npm test` for the gallery, cache, and signed-webhook regression tests.

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

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
