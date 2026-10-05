# Connect GitHub to an ADP organization

Create ADP organizations and assign people and teams in ADP. A GitHub account's name and ownership do not create an ADP organization, select an organization, or grant ADP membership.

1. Register or import the deployment's GitHub App in Settings → Connections.
2. Select the ADP organization that should use the repositories.
3. Choose **Install on GitHub** and select repositories. If the App is already installed, expand **Already installed on GitHub?**, enter the installation ID from the GitHub installation settings URL, and choose **Connect to selected ADP organization**.

Connecting requires the existing verified GitHub identity and installation-control checks. The setup link binds the signed-in user and selected ADP organization. Changing GitHub account metadata cannot change that target. Existing installations owned by a different ADP organization remain protected against reassignment.

An installation started directly from GitHub has no ADP organization selection. Its callback directs the user back to ADP to complete the connection; it creates no ADP organization or membership. This also applies to deployments that previously enabled `ORG_TENANT_AUTO_CREATE`.

The Connections page groups installations by ADP organization and shows the GitHub account name on each installation card. One installation still has one owning ADP organization. Existing legacy mappings require an explicit operator repair; an upgrade does not guess a destination or move installations automatically.
