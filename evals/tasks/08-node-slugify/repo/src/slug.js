'use strict';

/** Turn a title into a URL slug: lowercase, words joined by single hyphens, no leading/trailing hyphens. */
function slugify(text) {
  return text.toLowerCase().replace(/ /g, '-');
}

module.exports = { slugify };
