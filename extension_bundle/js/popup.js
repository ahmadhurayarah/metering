function normalizeProfileId(value) {
  return value.trim().toLowerCase();
}

document.getElementById("addprofilebtn").addEventListener("click", function () {
  var query = normalizeProfileId(document.getElementById("profileid").value);
  window.open("https://www.google.com/maps/search/" + encodeURIComponent(query));
});
