import { setupServer } from "msw/node";

// Empty by default: each test registers its own handlers via server.use(...).
// Unhandled requests error out (see setup.ts) so a missing mock is loud.
export const server = setupServer();
